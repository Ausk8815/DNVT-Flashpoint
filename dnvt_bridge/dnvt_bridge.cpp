/**
 * DNVT USB Bridge — native C++ implementation.
 *
 * Runs a dedicated thread for USB I/O at ~1ms poll rate.
 * CVSD decode/encode and 32k<->8k resampling happen in the same thread.
 * Audio is exchanged with Python via lock-free ring buffers.
 */

#include "dnvt_bridge.h"
#include "cvsd_codec/cvsd_codec.h"

#include <libusb-1.0/libusb.h>

#include <cstring>
#include <cmath>
#include <atomic>
#include <thread>
#include <mutex>
#include <chrono>
#include <cstdio>

// ============================================================================
// Constants
// ============================================================================

static const uint16_t USB_VID = 0xCAFE;
static const uint16_t USB_PID = 0x6942;
static const uint8_t  EP_IN   = 0x82;
static const uint8_t  EP_OUT  = 0x01;
static const int      IFACE   = 0;

static const uint32_t NULL_AUDIO = 0xAAAAAAAA;

// Ring buffer sizes (in samples at 8kHz)
// 8000 samples = 1 second of audio — plenty of headroom
static const int RING_BUF_SIZE = 8192;  // must be power of 2

// HOST_PACKET: 56 bytes
// DEVICE_PACKET: 53 bytes

#pragma pack(push, 1)
struct HostPacket {
    uint16_t phone_states;     // 4 phones × 4 bits
    uint8_t  data_lengths;     // 4 phones × 2 bits
    uint8_t  reserved;
    uint32_t data[4][3];       // up to 3 words per phone
    uint8_t  phone_digits[4];
};
static_assert(sizeof(HostPacket) == 56, "HostPacket size mismatch");

struct DevicePacket {
    uint32_t data[4][3];       // up to 3 words per phone
    uint8_t  data_lengths;     // 4 phones × 2 bits
    uint8_t  phone_commands[4];
};
static_assert(sizeof(DevicePacket) == 53, "DevicePacket size mismatch");
#pragma pack(pop)

// ============================================================================
// Lock-free ring buffer (SPSC — single producer, single consumer)
// ============================================================================

struct RingBuf {
    int16_t  buf[RING_BUF_SIZE];
    std::atomic<int> head{0};  // write position (producer)
    std::atomic<int> tail{0};  // read position (consumer)

    int available() const {
        int h = head.load(std::memory_order_acquire);
        int t = tail.load(std::memory_order_acquire);
        return (h - t + RING_BUF_SIZE) & (RING_BUF_SIZE - 1);
    }

    int space() const {
        return RING_BUF_SIZE - 1 - available();
    }

    int write(const int16_t* data, int count) {
        int avail_space = space();
        if (count > avail_space) count = avail_space;
        int h = head.load(std::memory_order_relaxed);
        for (int i = 0; i < count; i++) {
            buf[(h + i) & (RING_BUF_SIZE - 1)] = data[i];
        }
        head.store((h + count) & (RING_BUF_SIZE - 1), std::memory_order_release);
        return count;
    }

    int read(int16_t* data, int count) {
        int avail = available();
        if (count > avail) count = avail;
        int t = tail.load(std::memory_order_relaxed);
        for (int i = 0; i < count; i++) {
            data[i] = buf[(t + i) & (RING_BUF_SIZE - 1)];
        }
        tail.store((t + count) & (RING_BUF_SIZE - 1), std::memory_order_release);
        return count;
    }

    void clear() {
        tail.store(head.load(std::memory_order_acquire), std::memory_order_release);
    }
};

// ============================================================================
// Per-phone state
// ============================================================================

struct PhoneLine {
    // Audio ring buffers (8kHz PCM)
    RingBuf rx_ring;   // DNVT -> host (decoded from phone)
    RingBuf tx_ring;   // host -> DNVT (to encode for phone)

    // CVSD codec state
    CvsdStreamDecoder* decoder = nullptr;
    CvsdStreamEncoder* encoder = nullptr;

    // Status
    std::atomic<uint8_t> state{PHONE_IDLE};
    std::atomic<char>    digit{0};
    std::atomic<uint8_t> raw_state{0};

    // Pending command (written by Python, consumed by I/O thread)
    std::atomic<uint8_t> pending_cmd{CMD_NONE};

    // Stats
    std::atomic<uint16_t> rx_word_count{0};
    std::atomic<uint16_t> tx_word_count{0};

    // Partial encoder output buffer (words waiting to be sent)
    uint32_t tx_word_buf[64];
    int      tx_word_buf_count = 0;
    int      tx_word_buf_pos = 0;

    // Intercom: raw CVSD word ring buffer (bypasses PCM encode/decode)
    static const int ICOM_BUF_SIZE = 256;
    uint32_t icom_rx_buf[256];  // raw words received from this phone
    std::atomic<int> icom_rx_head{0};
    std::atomic<int> icom_rx_tail{0};
    std::atomic<int> icom_partner{-1};  // -1 = no intercom, 0-3 = partner phone

    // Idle TX rate limiter
    std::chrono::steady_clock::time_point last_idle_tx = std::chrono::steady_clock::now();

    // Tone playback (pre-encoded CVSD words, looped)
    uint32_t* tone_words = nullptr;
    int       tone_len = 0;
    int       tone_pos = 0;
    std::atomic<bool> tone_playing{false};
};

// ============================================================================
// Global state
// ============================================================================

static std::atomic<bool> g_running{false};
static std::thread       g_io_thread;
static libusb_context*   g_ctx = nullptr;
static libusb_device_handle* g_dev = nullptr;
static PhoneLine         g_phones[NUM_PHONES];

// Debug: dump decoded audio directly from I/O thread
static FILE* g_debug_wav = nullptr;
static int   g_debug_samples = 0;

// ============================================================================
// I/O thread
// ============================================================================

static void io_thread_func()
{
    printf("[bridge] I/O thread started\n");

    int pkt_count = 0;
    int total_rx_words = 0;
    int out_pkt_count = 0;
    uint8_t raw_in[64];
    uint8_t raw_out[64];
    auto start_time = std::chrono::steady_clock::now();

    // Temp buffers for CVSD decode/encode
    int16_t  pcm_buf[32 * 3];       // max 3 words × 32 samples = 96 samples at 32k
    uint32_t enc_buf[16];            // encoded words output
    int16_t  resample_buf[96];       // resampled output

    // Send initial keepalive
    memset(raw_out, 0, sizeof(raw_out));
    int transferred = 0;
    libusb_bulk_transfer(g_dev, EP_OUT, raw_out, 53, &transferred, 100);

    while (g_running.load(std::memory_order_relaxed))
    {
        // ---- READ HOST_PACKET ----
        transferred = 0;
        int rc = libusb_bulk_transfer(g_dev, EP_IN, raw_in, 64, &transferred, 2);

        // Don't spin faster than ~1000/sec to reduce firmware USB interrupt load
        // The firmware produces ~1 word per ms, so reading faster is wasteful
        std::this_thread::sleep_for(std::chrono::microseconds(500));

        if (rc == LIBUSB_ERROR_TIMEOUT || transferred < (int)sizeof(HostPacket)) {
            // No data — still need to send TX if we have audio
            // Fall through to TX processing
        }

        HostPacket* hp = nullptr;
        if (rc == 0 && transferred >= (int)sizeof(HostPacket)) {
            hp = reinterpret_cast<HostPacket*>(raw_in);

            for (int p = 0; p < NUM_PHONES; p++) {
                PhoneLine& ph = g_phones[p];

                // Update state
                uint8_t mapped = (hp->phone_states >> (p * 4)) & 0xF;
                ph.state.store(mapped, std::memory_order_relaxed);
                ph.raw_state.store(hp->reserved & 0x0F, std::memory_order_relaxed);

                // Digit
                if (hp->phone_digits[p] != 0) {
                    ph.digit.store(hp->phone_digits[p], std::memory_order_relaxed);
                }

                // Decode RX audio (DNVT -> host)
                int dl = (hp->data_lengths >> (p * 2)) & 0x3;
                if (dl > 0) {
                    uint32_t* words = hp->data[p];
                    int n_words = dl;

                    // Intercom: forward raw words to partner's icom buffer
                    int partner = ph.icom_partner.load(std::memory_order_relaxed);
                    if (partner >= 0 && partner < NUM_PHONES) {
                        PhoneLine& dest = g_phones[partner];
                        for (int j = 0; j < n_words; j++) {
                            int h = dest.icom_rx_head.load(std::memory_order_relaxed);
                            int next = (h + 1) % PhoneLine::ICOM_BUF_SIZE;
                            if (next != dest.icom_rx_tail.load(std::memory_order_acquire)) {
                                dest.icom_rx_buf[h] = words[j];
                                dest.icom_rx_head.store(next, std::memory_order_release);
                            }
                        }
                    }

                    // Normal decode to PCM (for SIP or speaker test)
                    if (ph.decoder) {
                        int pcm_len = 0;
                        cvsd_stream_decode_words(ph.decoder, words, n_words, pcm_buf, &pcm_len);

                        // Decimate 32kHz -> 8kHz (4-tap box filter to reduce aliasing)
                        int n_8k = pcm_len / 4;
                        for (int i = 0; i < n_8k; i++) {
                            int sum = pcm_buf[i*4] + pcm_buf[i*4+1] + pcm_buf[i*4+2] + pcm_buf[i*4+3];
                            resample_buf[i] = (int16_t)(sum / 4);
                        }

                        ph.rx_ring.write(resample_buf, n_8k);
                        ph.rx_word_count.fetch_add(n_words, std::memory_order_relaxed);

                        if (p == 0 && g_debug_wav) {
                            fwrite(resample_buf, sizeof(int16_t), n_8k, g_debug_wav);
                            g_debug_samples += n_8k;
                        }
                        if (p == 0) total_rx_words += n_words;
                    }
                }
            }
        }

        // ---- BUILD AND SEND DEVICE_PACKET ----
        pkt_count++;

        DevicePacket dp;
        memset(&dp, 0, sizeof(dp));

        bool have_data = false;

        for (int p = 0; p < NUM_PHONES; p++) {
            PhoneLine& ph = g_phones[p];

            // Pending command
            uint8_t cmd = ph.pending_cmd.exchange(CMD_NONE, std::memory_order_relaxed);
            if (cmd != CMD_NONE) {
                dp.phone_commands[p] = cmd;
                have_data = true;
            }

            // Encode TX audio (host -> DNVT)
            // Rate-match: send exactly as many words as we received from this phone
            int rx_dl = 0;
            if (hp) {
                rx_dl = (hp->data_lengths >> (p * 2)) & 0x3;
            }
            int want = rx_dl;

            // Fill tx_word_buf from ring buffer if needed
            while (ph.tx_word_buf_pos >= ph.tx_word_buf_count) {
                // Need more encoded words — pull PCM from ring buffer
                int avail = ph.tx_ring.available();
                if (avail < 8) break;  // need at least 8 samples (= 32 at 32k = 1 word)

                // Read up to 24 samples at 8kHz (= 3 words worth)
                int16_t pcm_8k[24];
                int n_read = ph.tx_ring.read(pcm_8k, 24);
                if (n_read <= 0) break;

                // Upsample 8kHz -> 32kHz (repeat each sample 4x)
                int16_t pcm_32k[96];
                int n_32k = n_read * 4;
                for (int i = 0; i < n_read; i++) {
                    pcm_32k[i*4+0] = pcm_8k[i];
                    pcm_32k[i*4+1] = pcm_8k[i];
                    pcm_32k[i*4+2] = pcm_8k[i];
                    pcm_32k[i*4+3] = pcm_8k[i];
                }

                // Encode to CVSD words
                if (ph.encoder) {
                    int enc_len = 0;
                    cvsd_stream_encode_words(ph.encoder, pcm_32k, n_32k,
                                             ph.tx_word_buf, &enc_len);
                    ph.tx_word_buf_count = enc_len;
                    ph.tx_word_buf_pos = 0;
                }
            }

            // Pack words into DevicePacket
            int n_send = 0;
            int partner = ph.icom_partner.load(std::memory_order_relaxed);
            if (partner >= 0 && partner < NUM_PHONES) {
                // Intercom mode: pull raw words from partner's icom buffer
                for (int j = 0; j < want && j < 3; j++) {
                    int t = ph.icom_rx_tail.load(std::memory_order_relaxed);
                    int h = ph.icom_rx_head.load(std::memory_order_acquire);
                    if (t != h) {
                        dp.data[p][j] = ph.icom_rx_buf[t];
                        ph.icom_rx_tail.store((t + 1) % PhoneLine::ICOM_BUF_SIZE, std::memory_order_release);
                        n_send++;
                    }
                }
            } else {
                // Normal mode: send encoded PCM words
                for (int j = 0; j < want && j < 3; j++) {
                    if (ph.tx_word_buf_pos < ph.tx_word_buf_count) {
                        dp.data[p][j] = ph.tx_word_buf[ph.tx_word_buf_pos++];
                        n_send++;
                    }
                }
            }
            if (n_send > 0) {
                dp.data_lengths |= (n_send << (p * 2));
                ph.tx_word_count.fetch_add(n_send, std::memory_order_relaxed);
                have_data = true;
            }
        }

        // Periodic stats — every second
        {
            static auto last_stat_time = std::chrono::steady_clock::now();
            static int last_rx_words = 0;
            static int last_pkt_count = 0;
            auto now = std::chrono::steady_clock::now();
            double dt = std::chrono::duration<double>(now - last_stat_time).count();
            if (dt >= 1.0) {
                int delta_rx = total_rx_words - last_rx_words;
                int delta_pkt = pkt_count - last_pkt_count;
                uint8_t fw_raw = hp ? hp->reserved : 0xFF;
                uint8_t fw_state = fw_raw & 0x0F;
                uint8_t fw_txq = (fw_raw >> 4) & 0x0F;
                uint8_t mapped = hp ? ((hp->phone_states) & 0xF) : 0xFF;
                printf("[bridge] IN=%d/s OUT=%d/s rx_words=%d/s (=%d samp/s at 8k) fw=%d map=%d txq=%d\n",
                       delta_pkt, out_pkt_count, delta_rx, delta_rx * 8, fw_state, mapped, fw_txq);
                last_stat_time = now;
                last_rx_words = total_rx_words;
                last_pkt_count = pkt_count;
                out_pkt_count = 0;
            }
        }

        // Throttle OUT packets to avoid starving firmware CPU with USB interrupts
        // Send when we have real data (commands/audio) or every ~10ms for keepalive
        if (have_data || (pkt_count % 10) == 0) {
            transferred = 0;
            memcpy(raw_out, &dp, sizeof(dp));
            libusb_bulk_transfer(g_dev, EP_OUT, raw_out, 53, &transferred, 5);
            out_pkt_count++;
        }
    }

    printf("[bridge] I/O thread stopped\n");
}

// ============================================================================
// Public API
// ============================================================================

BRIDGE_API int bridge_init(void)
{
    if (g_running.load()) return -1;  // already running

    // Init libusb
    int rc = libusb_init(&g_ctx);
    if (rc != 0) {
        fprintf(stderr, "[bridge] libusb_init failed: %s\n", libusb_strerror((libusb_error)rc));
        return -1;
    }

    // Open device
    g_dev = libusb_open_device_with_vid_pid(g_ctx, USB_VID, USB_PID);
    if (!g_dev) {
        fprintf(stderr, "[bridge] Device not found (VID=%04X PID=%04X)\n", USB_VID, USB_PID);
        libusb_exit(g_ctx);
        g_ctx = nullptr;
        return -2;
    }

    // Detach kernel driver if needed (Linux)
    #ifndef _WIN32
    if (libusb_kernel_driver_active(g_dev, IFACE) == 1) {
        libusb_detach_kernel_driver(g_dev, IFACE);
    }
    #endif

    rc = libusb_claim_interface(g_dev, IFACE);
    if (rc != 0) {
        fprintf(stderr, "[bridge] claim interface failed: %s\n", libusb_strerror((libusb_error)rc));
        libusb_close(g_dev);
        libusb_exit(g_ctx);
        g_dev = nullptr;
        g_ctx = nullptr;
        return -3;
    }

    // Init per-phone codec state
    for (int i = 0; i < NUM_PHONES; i++) {
        g_phones[i].decoder = cvsd_stream_decoder_create(0);  // exponential
        g_phones[i].encoder = cvsd_stream_encoder_create();
        g_phones[i].rx_ring.clear();
        g_phones[i].tx_ring.clear();
        g_phones[i].tx_word_buf_count = 0;
        g_phones[i].tx_word_buf_pos = 0;
        g_phones[i].state.store(PHONE_IDLE);
        g_phones[i].digit.store(0);
        g_phones[i].pending_cmd.store(CMD_NONE);
        g_phones[i].rx_word_count.store(0);
        g_phones[i].tx_word_count.store(0);
    }

    // Debug: open raw dump file
    g_debug_wav = fopen("dll_debug_raw.pcm", "wb");
    g_debug_samples = 0;

    // Start I/O thread
    g_running.store(true);
    g_io_thread = std::thread(io_thread_func);

    printf("[bridge] Initialized — USB device opened, I/O thread running\n");
    return 0;
}

BRIDGE_API void bridge_shutdown(void)
{
    if (!g_running.load()) return;

    g_running.store(false);
    if (g_io_thread.joinable())
        g_io_thread.join();

    // Clean up codecs
    for (int i = 0; i < NUM_PHONES; i++) {
        if (g_phones[i].decoder) {
            cvsd_stream_decoder_destroy(g_phones[i].decoder);
            g_phones[i].decoder = nullptr;
        }
        if (g_phones[i].encoder) {
            cvsd_stream_encoder_destroy(g_phones[i].encoder);
            g_phones[i].encoder = nullptr;
        }
    }

    // Debug: close dump file
    if (g_debug_wav) {
        fclose(g_debug_wav);
        printf("[bridge] Debug dump: %d samples written to dll_debug_raw.pcm\n", g_debug_samples);
        g_debug_wav = nullptr;
    }

    if (g_dev) {
        libusb_release_interface(g_dev, IFACE);
        libusb_close(g_dev);
        g_dev = nullptr;
    }
    if (g_ctx) {
        libusb_exit(g_ctx);
        g_ctx = nullptr;
    }

    printf("[bridge] Shutdown complete\n");
}

BRIDGE_API int bridge_is_running(void)
{
    return g_running.load() ? 1 : 0;
}

BRIDGE_API void bridge_get_status(PhoneStatus* status)
{
    for (int i = 0; i < NUM_PHONES; i++) {
        status[i].state = g_phones[i].state.load(std::memory_order_relaxed);
        status[i].digit = g_phones[i].digit.load(std::memory_order_relaxed);
        status[i].raw_state = g_phones[i].raw_state.load(std::memory_order_relaxed);
        status[i].rx_words = g_phones[i].rx_word_count.exchange(0, std::memory_order_relaxed);
        status[i].tx_words = g_phones[i].tx_word_count.exchange(0, std::memory_order_relaxed);
    }
}

BRIDGE_API void bridge_send_command(int phone, uint8_t cmd)
{
    if (phone < 0 || phone >= NUM_PHONES) return;
    g_phones[phone].pending_cmd.store(cmd, std::memory_order_relaxed);
}

BRIDGE_API int bridge_get_audio_8k(int phone, int16_t* buf, int max_samples)
{
    if (phone < 0 || phone >= NUM_PHONES || !buf || max_samples <= 0) return 0;
    return g_phones[phone].rx_ring.read(buf, max_samples);
}

BRIDGE_API int bridge_put_audio_8k(int phone, const int16_t* buf, int num_samples)
{
    if (phone < 0 || phone >= NUM_PHONES || !buf || num_samples <= 0) return 0;
    return g_phones[phone].tx_ring.write(buf, num_samples);
}

BRIDGE_API void bridge_set_intercom(int phone_a, int phone_b)
{
    // Connect two phones for raw CVSD intercom. Pass phone_b=-1 to disconnect.
    if (phone_a < 0 || phone_a >= NUM_PHONES) return;
    if (phone_b >= NUM_PHONES) return;

    // Disconnect old partners
    int old_b = g_phones[phone_a].icom_partner.load(std::memory_order_relaxed);
    if (old_b >= 0 && old_b < NUM_PHONES) {
        g_phones[old_b].icom_partner.store(-1, std::memory_order_relaxed);
    }
    if (phone_b >= 0) {
        int old_a = g_phones[phone_b].icom_partner.load(std::memory_order_relaxed);
        if (old_a >= 0 && old_a < NUM_PHONES) {
            g_phones[old_a].icom_partner.store(-1, std::memory_order_relaxed);
        }
    }

    // Set new partners (bidirectional)
    g_phones[phone_a].icom_partner.store(phone_b, std::memory_order_relaxed);
    if (phone_b >= 0) {
        g_phones[phone_b].icom_partner.store(phone_a, std::memory_order_relaxed);
        // Clear intercom buffers
        g_phones[phone_a].icom_rx_head.store(0, std::memory_order_relaxed);
        g_phones[phone_a].icom_rx_tail.store(0, std::memory_order_relaxed);
        g_phones[phone_b].icom_rx_head.store(0, std::memory_order_relaxed);
        g_phones[phone_b].icom_rx_tail.store(0, std::memory_order_relaxed);
    }
}

BRIDGE_API char bridge_get_digit(int phone)
{
    if (phone < 0 || phone >= NUM_PHONES) return 0;
    return g_phones[phone].digit.exchange(0, std::memory_order_relaxed);
}

BRIDGE_API void bridge_clear_audio(int phone)
{
    if (phone < 0 || phone >= NUM_PHONES) return;
    g_phones[phone].rx_ring.clear();
    g_phones[phone].tx_ring.clear();
    g_phones[phone].tx_word_buf_count = 0;
    g_phones[phone].tx_word_buf_pos = 0;
    // Reset codec state too
    if (g_phones[phone].decoder) cvsd_stream_decoder_reset(g_phones[phone].decoder);
    if (g_phones[phone].encoder) cvsd_stream_encoder_reset(g_phones[phone].encoder);
}
