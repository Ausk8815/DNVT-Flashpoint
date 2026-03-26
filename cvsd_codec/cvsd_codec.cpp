// 32 kbps CVSD Codec for Digital Non-secure Voice Terminals
// C++ port of Python implementations by Nick Andre and Robert Ruark (2023)

#include "cvsd_codec.h"
#include <cmath>
#include <cstring>
#include <algorithm>

// ============================================================================
// Encoder — matches encoder.py exactly
// ============================================================================

static constexpr double ENC_SCALE       = 250.0;
static constexpr double ENC_MAX_GAIN    = 20.0;
static constexpr double ENC_GAIN_STEP   = 0.72;
static constexpr double ENC_SIG_DECAY   = 0.98;
static constexpr double ENC_GAIN_DECAY  = 0.9875778;

CVSD_API void cvsd_encode(const int16_t* pcm_in, int num_samples,
                          uint8_t* cvsd_out, int* out_len)
{
    double current_value = 0.0;
    int    coincidence_counter = 0;
    double gain = 0.0;
    uint8_t sample = 0;

    // Emphasis disabled (a=0.0) so filtered_pcm_val == pcm_value
    int nibble_count = 0;

    for (int i = 0; i < num_samples; i++) {
        int bit_index = 3 - (i % 4);
        double pcm_value = static_cast<double>(pcm_in[i]);

        // Determine bit
        int current_bit = (pcm_value >= current_value) ? 1 : 0;
        sample |= (current_bit << bit_index);

        // Coincidence check — gain increase
        if (std::abs(coincidence_counter) >= 3) {
            if (gain < ENC_MAX_GAIN)
                gain += ENC_GAIN_STEP;
        }

        // Update coincidence counter and current value
        if (current_bit == 1) {
            if (coincidence_counter < 0)
                coincidence_counter = 1;
            else
                coincidence_counter += 1;
            current_value += (gain + 1.0) * ENC_SCALE;
        } else {
            if (coincidence_counter > 0)
                coincidence_counter = -1;
            else
                coincidence_counter -= 1;
            current_value -= (gain + 1.0) * ENC_SCALE;
        }

        current_value *= ENC_SIG_DECAY;
        gain *= ENC_GAIN_DECAY;

        // Emit nibble
        if (bit_index == 0) {
            cvsd_out[nibble_count++] = sample;
            sample = 0;
        }
    }

    // Flush partial nibble if num_samples not divisible by 4
    if (num_samples % 4 != 0) {
        cvsd_out[nibble_count++] = sample;
    }

    *out_len = nibble_count;
}

// ============================================================================
// Decoder (exponential averaging) — matches decoder_exp.py "rob" parameters
// ============================================================================

static constexpr double DEC_EXP_MIN_GAIN       = 100.0;
static constexpr double DEC_EXP_GAIN_FRACTION  = 0.3;
static constexpr int    DEC_EXP_GAIN_STEP      = 30;   // int(100*0.3)
static constexpr int    DEC_EXP_COINCIDENCE     = 3;
static constexpr double DEC_EXP_SIG_DECAY       = 0.96;
static constexpr double DEC_EXP_GAIN_DECAY      = 0.99;
static constexpr double DEC_EXP_MAX_GAIN        = 18000.0;

CVSD_API void cvsd_decode_exp(const uint8_t* cvsd_in, int num_nibbles,
                              int16_t* pcm_out, int* out_len)
{
    double current_value = 0.0;
    int    coincidence_counter = 0;
    double syllabic_gain = 0.0;

    int total_samples = num_nibbles * 4;

    // First pass: decode to raw double buffer (stack-unfriendly for huge data, use heap)
    double* raw = new double[total_samples];
    int idx = 0;

    for (int n = 0; n < num_nibbles; n++) {
        uint8_t nibble = cvsd_in[n];
        for (int i = 0; i < 4; i++) {
            int bitmask = 1 << (3 - i);
            int current_bit = (nibble & bitmask) ? 1 : 0;

            // Coincidence counting
            if (current_bit == 1) {
                if (coincidence_counter < 0)
                    coincidence_counter = 1;
                else
                    coincidence_counter += 1;
            } else {
                if (coincidence_counter > 0)
                    coincidence_counter = -1;
                else
                    coincidence_counter -= 1;
            }

            // Syllabic gain update
            if (std::abs(coincidence_counter) >= DEC_EXP_COINCIDENCE) {
                if (syllabic_gain < DEC_EXP_MAX_GAIN)
                    syllabic_gain += DEC_EXP_GAIN_STEP;
            }
            double gain = DEC_EXP_MIN_GAIN + syllabic_gain;

            // Update signal
            if (current_bit == 1)
                current_value += gain;
            else
                current_value -= gain;

            // Clip
            if (current_value > 32767.0)  current_value = 32767.0;
            if (current_value < -32768.0) current_value = -32768.0;

            raw[idx++] = current_value;

            // Decay
            current_value *= DEC_EXP_SIG_DECAY;
            syllabic_gain *= DEC_EXP_GAIN_DECAY;
        }
    }

    // Second pass: 3-tap exponential averaging filter
    double prevval1 = 0.0, prevval2 = 0.0;
    for (int i = 0; i < total_samples; i++) {
        double filtval = 0.33 * prevval1 + 0.33 * prevval2 + 0.34 * raw[i];
        prevval2 = prevval1;
        prevval1 = raw[i];

        // Clamp to int16 range
        int32_t out_val = static_cast<int32_t>(filtval);
        if (out_val > 32767)  out_val = 32767;
        if (out_val < -32768) out_val = -32768;
        pcm_out[i] = static_cast<int16_t>(out_val);
    }

    delete[] raw;
    *out_len = total_samples;
}

// ============================================================================
// IIR filter — Chebyshev Type II, 6th order, 5kHz cutoff @ 32kHz
// Precomputed from: signal.iirfilter(6, [5000], rs=40, btype='lowpass',
//                   ftype='cheby2', fs=32000, output='sos')
// 3 second-order sections, each row: [b0, b1, b2, a0, a1, a2]
// ============================================================================

static constexpr int    IIR_NUM_SECTIONS = 3;
static const double IIR_SOS[3][6] = {
    { 0.02215132175550254,  0.02747359253561335,  0.02215132175550254,
      1.0,                 -0.6570852441558558,   0.13475223303530415 },
    { 1.0,                 -0.545493847329796,    0.9999999999999999,
      1.0,                 -0.9568060743635728,   0.4052911048647915  },
    { 1.0,                 -1.0622838378728612,   1.0,
      1.0,                 -1.321222548785673,    0.7781996767218764  }
};

// Direct-form II transposed cascaded second-order sections filter
// Processes one sample through all sections, maintaining state in zi[sections][2]
static inline double sosfilt_sample(double x, double zi[][2])
{
    double val = x;
    for (int s = 0; s < IIR_NUM_SECTIONS; s++) {
        const double* c = IIR_SOS[s];
        double b0 = c[0], b1 = c[1], b2 = c[2];
        double a1 = c[4], a2 = c[5];  // a0 is always 1.0

        // Direct-form II transposed
        double y_out = b0 * val + zi[s][0];
        zi[s][0] = b1 * val - a1 * y_out + zi[s][1];
        zi[s][1] = b2 * val - a2 * y_out;
        val = y_out;
    }
    return val;
}

// ============================================================================
// Decoder (IIR) — matches decoder_iir.py
// ============================================================================

CVSD_API void cvsd_decode_iir(const uint8_t* cvsd_in, int num_nibbles,
                              int16_t* pcm_out, int* out_len)
{
    double current_value = 0.0;
    int    coincidence_counter = 0;
    double gain = 0.0;
    double previous_filtered = 0.0;

    // IIR filter state (zero-initialized)
    double zi[IIR_NUM_SECTIONS][2] = {};

    int idx = 0;

    for (int n = 0; n < num_nibbles; n++) {
        uint8_t nibble = cvsd_in[n];
        for (int i = 0; i < 4; i++) {
            int bitmask = 1 << (3 - i);
            int current_bit = (nibble & bitmask) ? 1 : 0;

            // Coincidence check
            if (std::abs(coincidence_counter) >= 3) {
                if (gain < ENC_MAX_GAIN)
                    gain += ENC_GAIN_STEP;
            }

            // Update coincidence counter and signal
            if (current_bit == 1) {
                if (coincidence_counter < 0)
                    coincidence_counter = 1;
                else
                    coincidence_counter += 1;
                current_value += (gain + 1.0) * ENC_SCALE;
            } else {
                if (coincidence_counter > 0)
                    coincidence_counter = -1;
                else
                    coincidence_counter -= 1;
                current_value -= (gain + 1.0) * ENC_SCALE;
            }

            current_value *= ENC_SIG_DECAY;
            gain *= ENC_GAIN_DECAY;

            // De-emphasis IIR: y[n] = (x[n] + a*y[n-1]) / 2
            double a = 0.90;
            double filtered_val = (current_value + previous_filtered * a) / 2.0;
            previous_filtered = filtered_val;

            // Chebyshev Type II lowpass
            double fir_val = sosfilt_sample(filtered_val, zi);

            // Clamp
            if (fir_val > 32767.0)  fir_val = 32767.0;
            if (fir_val < -32768.0) fir_val = -32768.0;

            pcm_out[idx++] = static_cast<int16_t>(static_cast<int32_t>(fir_val));
        }
    }

    *out_len = idx;
}

// ============================================================================
// Streaming decoder — stateful, for real-time audio
// ============================================================================

struct CvsdStreamDecoder {
    int decoder_type;  // 0 = exp, 1 = IIR

    // Shared CVSD state
    double current_value;
    int    coincidence_counter;
    double syllabic_gain;   // exp decoder
    double gain;            // IIR decoder

    // Exp decoder post-filter state
    double prevval1, prevval2;

    // IIR decoder state
    double previous_filtered;
    double zi[IIR_NUM_SECTIONS][2];
};

CVSD_API CvsdStreamDecoder* cvsd_stream_decoder_create(int decoder_type)
{
    auto* dec = new CvsdStreamDecoder();
    dec->decoder_type = decoder_type;
    dec->current_value = 0.0;
    dec->coincidence_counter = 0;
    dec->syllabic_gain = 0.0;
    dec->gain = 0.0;
    dec->prevval1 = 0.0;
    dec->prevval2 = 0.0;
    dec->previous_filtered = 0.0;
    std::memset(dec->zi, 0, sizeof(dec->zi));
    return dec;
}

CVSD_API void cvsd_stream_decoder_reset(CvsdStreamDecoder* dec)
{
    if (!dec) return;
    dec->current_value = 0.0;
    dec->coincidence_counter = 0;
    dec->syllabic_gain = 0.0;
    dec->gain = 0.0;
    dec->prevval1 = 0.0;
    dec->prevval2 = 0.0;
    dec->previous_filtered = 0.0;
    std::memset(dec->zi, 0, sizeof(dec->zi));
}

CVSD_API void cvsd_stream_decoder_destroy(CvsdStreamDecoder* dec)
{
    delete dec;
}

// Process one CVSD bit through the exponential decoder, returning a PCM sample
static inline int16_t decode_bit_exp(CvsdStreamDecoder* dec, int current_bit)
{
    // Coincidence counting
    if (current_bit == 1) {
        if (dec->coincidence_counter < 0)
            dec->coincidence_counter = 1;
        else
            dec->coincidence_counter += 1;
    } else {
        if (dec->coincidence_counter > 0)
            dec->coincidence_counter = -1;
        else
            dec->coincidence_counter -= 1;
    }

    // Syllabic gain update
    if (std::abs(dec->coincidence_counter) >= DEC_EXP_COINCIDENCE) {
        if (dec->syllabic_gain < DEC_EXP_MAX_GAIN)
            dec->syllabic_gain += DEC_EXP_GAIN_STEP;
    }
    double gain = DEC_EXP_MIN_GAIN + dec->syllabic_gain;

    // Update signal
    if (current_bit == 1)
        dec->current_value += gain;
    else
        dec->current_value -= gain;

    // Clip
    if (dec->current_value > 32767.0)  dec->current_value = 32767.0;
    if (dec->current_value < -32768.0) dec->current_value = -32768.0;

    double raw = dec->current_value;

    // Decay
    dec->current_value *= DEC_EXP_SIG_DECAY;
    dec->syllabic_gain *= DEC_EXP_GAIN_DECAY;

    // 3-tap averaging filter
    double filtval = 0.33 * dec->prevval1 + 0.33 * dec->prevval2 + 0.34 * raw;
    dec->prevval2 = dec->prevval1;
    dec->prevval1 = raw;

    int32_t out_val = static_cast<int32_t>(filtval);
    if (out_val > 32767)  out_val = 32767;
    if (out_val < -32768) out_val = -32768;
    return static_cast<int16_t>(out_val);
}

// Process one CVSD bit through the IIR decoder, returning a PCM sample
static inline int16_t decode_bit_iir(CvsdStreamDecoder* dec, int current_bit)
{
    // Coincidence check
    if (std::abs(dec->coincidence_counter) >= 3) {
        if (dec->gain < ENC_MAX_GAIN)
            dec->gain += ENC_GAIN_STEP;
    }

    // Update coincidence counter and signal
    if (current_bit == 1) {
        if (dec->coincidence_counter < 0)
            dec->coincidence_counter = 1;
        else
            dec->coincidence_counter += 1;
        dec->current_value += (dec->gain + 1.0) * ENC_SCALE;
    } else {
        if (dec->coincidence_counter > 0)
            dec->coincidence_counter = -1;
        else
            dec->coincidence_counter -= 1;
        dec->current_value -= (dec->gain + 1.0) * ENC_SCALE;
    }

    dec->current_value *= ENC_SIG_DECAY;
    dec->gain *= ENC_GAIN_DECAY;

    // De-emphasis + IIR lowpass
    double a = 0.90;
    double filtered_val = (dec->current_value + dec->previous_filtered * a) / 2.0;
    dec->previous_filtered = filtered_val;

    double fir_val = sosfilt_sample(filtered_val, dec->zi);

    if (fir_val > 32767.0)  fir_val = 32767.0;
    if (fir_val < -32768.0) fir_val = -32768.0;

    return static_cast<int16_t>(static_cast<int32_t>(fir_val));
}

CVSD_API void cvsd_stream_decode_words(CvsdStreamDecoder* dec,
                                       const uint32_t* words_in, int num_words,
                                       int16_t* pcm_out, int* out_len)
{
    if (!dec || !words_in || !pcm_out || !out_len) {
        if (out_len) *out_len = 0;
        return;
    }

    int idx = 0;
    for (int w = 0; w < num_words; w++) {
        uint32_t word = words_in[w];
        // 32 bits per word, MSB-first
        for (int b = 31; b >= 0; b--) {
            int bit = (word >> b) & 1;
            if (dec->decoder_type == 0)
                pcm_out[idx++] = decode_bit_exp(dec, bit);
            else
                pcm_out[idx++] = decode_bit_iir(dec, bit);
        }
    }
    *out_len = idx;
}

// ============================================================================
// Streaming encoder — stateful, for real-time audio
// ============================================================================

struct CvsdStreamEncoder {
    double current_value;
    int    coincidence_counter;
    double gain;            // syllabic gain (matches decoder_exp)
    int    bits_in_word;    // 0-31, tracks partial word
    uint32_t current_word;
};

CVSD_API CvsdStreamEncoder* cvsd_stream_encoder_create(void)
{
    auto* enc = new CvsdStreamEncoder();
    enc->current_value = 0.0;
    enc->coincidence_counter = 0;
    enc->gain = 0.0;
    enc->bits_in_word = 0;
    enc->current_word = 0;
    return enc;
}

CVSD_API void cvsd_stream_encoder_reset(CvsdStreamEncoder* enc)
{
    if (!enc) return;
    enc->current_value = 0.0;
    enc->coincidence_counter = 0;
    enc->gain = 0.0;
    enc->bits_in_word = 0;
    enc->current_word = 0;
}

CVSD_API void cvsd_stream_encoder_destroy(CvsdStreamEncoder* enc)
{
    delete enc;
}

CVSD_API void cvsd_stream_encode_words(CvsdStreamEncoder* enc,
                                       const int16_t* pcm_in, int num_samples,
                                       uint32_t* words_out, int* out_len)
{
    // Uses decoder_exp parameters so the phone's hardware CVSD decoder
    // (which the exp decoder was reverse-engineered to match) can decode it.
    if (!enc || !pcm_in || !words_out || !out_len) {
        if (out_len) *out_len = 0;
        return;
    }

    int word_count = 0;

    for (int i = 0; i < num_samples; i++) {
        double pcm_value = static_cast<double>(pcm_in[i]);

        // Determine bit
        int current_bit = (pcm_value >= enc->current_value) ? 1 : 0;

        // Pack MSB-first: bit 31 first, down to bit 0
        int bit_pos = 31 - enc->bits_in_word;
        if (current_bit)
            enc->current_word |= (1u << bit_pos);

        // Coincidence counting (same as decoder_exp)
        if (current_bit == 1) {
            if (enc->coincidence_counter < 0)
                enc->coincidence_counter = 1;
            else
                enc->coincidence_counter += 1;
        } else {
            if (enc->coincidence_counter > 0)
                enc->coincidence_counter = -1;
            else
                enc->coincidence_counter -= 1;
        }

        // Syllabic gain update (decoder_exp parameters)
        if (std::abs(enc->coincidence_counter) >= DEC_EXP_COINCIDENCE) {
            if (enc->gain < DEC_EXP_MAX_GAIN)
                enc->gain += DEC_EXP_GAIN_STEP;
        }
        double gain = DEC_EXP_MIN_GAIN + enc->gain;

        // Update signal (decoder_exp style)
        if (current_bit == 1)
            enc->current_value += gain;
        else
            enc->current_value -= gain;

        // Clip
        if (enc->current_value > 32767.0)  enc->current_value = 32767.0;
        if (enc->current_value < -32768.0) enc->current_value = -32768.0;

        // Decay (decoder_exp parameters)
        enc->current_value *= DEC_EXP_SIG_DECAY;
        enc->gain *= DEC_EXP_GAIN_DECAY;

        enc->bits_in_word++;
        if (enc->bits_in_word >= 32) {
            words_out[word_count++] = enc->current_word;
            enc->current_word = 0;
            enc->bits_in_word = 0;
        }
    }

    *out_len = word_count;
}

// ============================================================================
// Tone detector — Goertzel algorithm
// ============================================================================

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

static constexpr int MAX_TONE_FREQS = 16;

struct ToneDetector {
    int    num_freqs;
    int    sample_rate;
    int    block_size;
    double threshold;

    // Per-frequency Goertzel coefficients
    double coeff[MAX_TONE_FREQS];

    // Per-frequency running state
    double s1[MAX_TONE_FREQS];
    double s2[MAX_TONE_FREQS];

    // Sample accumulator for incomplete blocks
    int    samples_in_block;
};

CVSD_API ToneDetector* tone_detector_create(const double* freqs, int num_freqs,
                                            int sample_rate, int block_size,
                                            double threshold)
{
    if (num_freqs <= 0 || num_freqs > MAX_TONE_FREQS || block_size <= 0)
        return nullptr;

    auto* det = new ToneDetector();
    det->num_freqs = num_freqs;
    det->sample_rate = sample_rate;
    det->block_size = block_size;
    det->threshold = threshold;
    det->samples_in_block = 0;

    for (int i = 0; i < num_freqs; i++) {
        // Goertzel coefficient: 2 * cos(2*pi*k/N) where k = freq*N/sample_rate
        double k = freqs[i] * block_size / sample_rate;
        det->coeff[i] = 2.0 * std::cos(2.0 * M_PI * k / block_size);
        det->s1[i] = 0.0;
        det->s2[i] = 0.0;
    }

    return det;
}

CVSD_API int tone_detector_feed(ToneDetector* det,
                                const int16_t* pcm_in, int num_samples,
                                uint8_t* results)
{
    if (!det || !pcm_in || !results) return 0;

    // Initialize results to 0 (no tone)
    for (int f = 0; f < det->num_freqs; f++)
        results[f] = 0;

    int blocks_processed = 0;
    int pos = 0;

    while (pos < num_samples) {
        // Feed samples into Goertzel accumulators
        double sample = static_cast<double>(pcm_in[pos++]) / 32768.0;

        for (int f = 0; f < det->num_freqs; f++) {
            double s0 = sample + det->coeff[f] * det->s1[f] - det->s2[f];
            det->s2[f] = det->s1[f];
            det->s1[f] = s0;
        }

        det->samples_in_block++;

        // Block complete — compute magnitudes
        if (det->samples_in_block >= det->block_size) {
            blocks_processed++;

            for (int f = 0; f < det->num_freqs; f++) {
                // Goertzel magnitude squared (normalized)
                double s1v = det->s1[f];
                double s2v = det->s2[f];
                double mag_sq = s1v * s1v + s2v * s2v - det->coeff[f] * s1v * s2v;
                // Normalize by block size squared
                double mag = std::sqrt(mag_sq) / det->block_size;

                if (mag >= det->threshold)
                    results[f] = 1;

                // Reset accumulators for next block
                det->s1[f] = 0.0;
                det->s2[f] = 0.0;
            }

            det->samples_in_block = 0;
        }
    }

    return blocks_processed;
}

CVSD_API int tone_detector_feed_mag(ToneDetector* det,
                                    const int16_t* pcm_in, int num_samples,
                                    uint8_t* results, double* magnitudes)
{
    if (!det || !pcm_in || !results || !magnitudes) return 0;

    for (int f = 0; f < det->num_freqs; f++) {
        results[f] = 0;
        magnitudes[f] = 0.0;
    }

    int blocks_processed = 0;
    int pos = 0;

    while (pos < num_samples) {
        double sample = static_cast<double>(pcm_in[pos++]) / 32768.0;

        for (int f = 0; f < det->num_freqs; f++) {
            double s0 = sample + det->coeff[f] * det->s1[f] - det->s2[f];
            det->s2[f] = det->s1[f];
            det->s1[f] = s0;
        }

        det->samples_in_block++;

        if (det->samples_in_block >= det->block_size) {
            blocks_processed++;

            for (int f = 0; f < det->num_freqs; f++) {
                double s1v = det->s1[f];
                double s2v = det->s2[f];
                double mag_sq = s1v * s1v + s2v * s2v - det->coeff[f] * s1v * s2v;
                double mag = std::sqrt(mag_sq) / det->block_size;

                magnitudes[f] = mag;
                if (mag >= det->threshold)
                    results[f] = 1;

                det->s1[f] = 0.0;
                det->s2[f] = 0.0;
            }

            det->samples_in_block = 0;
        }
    }

    return blocks_processed;
}

CVSD_API void tone_detector_reset(ToneDetector* det)
{
    if (!det) return;
    for (int f = 0; f < det->num_freqs; f++) {
        det->s1[f] = 0.0;
        det->s2[f] = 0.0;
    }
    det->samples_in_block = 0;
}

CVSD_API void tone_detector_destroy(ToneDetector* det)
{
    delete det;
}
