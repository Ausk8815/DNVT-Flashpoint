#ifndef CVSD_CODEC_H
#define CVSD_CODEC_H

#include <stdint.h>

#ifdef _WIN32
    #define CVSD_API extern "C" __declspec(dllexport)
#else
    #define CVSD_API extern "C" __attribute__((visibility("default")))
#endif

// Encode PCM samples to CVSD nibbles.
// pcm_in:      array of signed 16-bit PCM samples (32 kHz mono)
// num_samples: number of PCM samples
// cvsd_out:    output buffer for packed nibbles (each byte = one nibble 0x0..0xF)
//              caller must allocate at least num_samples bytes
// out_len:     receives the number of nibbles written (= num_samples, since 1 bit per sample, 4 bits per nibble)
CVSD_API void cvsd_encode(const int16_t* pcm_in, int num_samples,
                          uint8_t* cvsd_out, int* out_len);

// Decode CVSD nibbles to PCM using exponential averaging filter ("rob" parameters).
// cvsd_in:     array of nibbles (each byte = one nibble 0x0..0xF)
// num_nibbles: number of nibbles
// pcm_out:     output buffer, caller must allocate at least num_nibbles * 4 * sizeof(int16_t)
// out_len:     receives the number of PCM samples written
CVSD_API void cvsd_decode_exp(const uint8_t* cvsd_in, int num_nibbles,
                              int16_t* pcm_out, int* out_len);

// Decode CVSD nibbles to PCM using IIR Chebyshev Type II lowpass filter.
// Same buffer conventions as cvsd_decode_exp.
CVSD_API void cvsd_decode_iir(const uint8_t* cvsd_in, int num_nibbles,
                              int16_t* pcm_out, int* out_len);

// ============================================================================
// Streaming decoder — maintains state between calls for real-time audio
// ============================================================================

// Opaque decoder handle
typedef struct CvsdStreamDecoder CvsdStreamDecoder;

// Create a streaming decoder. decoder_type: 0 = exponential, 1 = IIR
CVSD_API CvsdStreamDecoder* cvsd_stream_decoder_create(int decoder_type);

// Decode a chunk of raw 32-bit words (as received from the DNVT PIO).
// Each uint32 contains 32 CVSD bits, MSB-first.
// words_in:   array of uint32 words
// num_words:  number of words
// pcm_out:    output buffer, caller must allocate at least num_words * 32 * sizeof(int16_t)
// out_len:    receives number of PCM samples written
CVSD_API void cvsd_stream_decode_words(CvsdStreamDecoder* dec,
                                       const uint32_t* words_in, int num_words,
                                       int16_t* pcm_out, int* out_len);

// Reset decoder state (e.g. when phone goes idle)
CVSD_API void cvsd_stream_decoder_reset(CvsdStreamDecoder* dec);

// Free a streaming decoder
CVSD_API void cvsd_stream_decoder_destroy(CvsdStreamDecoder* dec);

// ============================================================================
// Streaming encoder — maintains state between calls for real-time audio
// ============================================================================

typedef struct CvsdStreamEncoder CvsdStreamEncoder;

// Create a streaming encoder
CVSD_API CvsdStreamEncoder* cvsd_stream_encoder_create(void);

// Encode PCM samples to raw 32-bit words (for DNVT PIO).
// Each uint32 contains 32 CVSD bits, MSB-first.
// pcm_in:    array of int16 PCM samples
// num_samples: number of samples (should be multiple of 32 for complete words)
// words_out: output buffer, caller must allocate at least (num_samples/32 + 1) uint32s
// out_len:   receives number of words written
CVSD_API void cvsd_stream_encode_words(CvsdStreamEncoder* enc,
                                       const int16_t* pcm_in, int num_samples,
                                       uint32_t* words_out, int* out_len);

// Reset encoder state
CVSD_API void cvsd_stream_encoder_reset(CvsdStreamEncoder* enc);

// Free a streaming encoder
CVSD_API void cvsd_stream_encoder_destroy(CvsdStreamEncoder* enc);

// ============================================================================
// Tone detector — Goertzel algorithm for real-time frequency detection
// ============================================================================

typedef struct ToneDetector ToneDetector;

// Create a tone detector.
// freqs:       array of frequencies to watch for (Hz)
// num_freqs:   number of frequencies (max 16)
// sample_rate: audio sample rate (e.g. 32000)
// block_size:  number of samples per detection window (e.g. 320 = 10ms at 32kHz)
// threshold:   magnitude threshold for tone-present (0.0-1.0, relative to full-scale)
CVSD_API ToneDetector* tone_detector_create(const double* freqs, int num_freqs,
                                            int sample_rate, int block_size,
                                            double threshold);

// Feed PCM samples into the detector. Processes in block_size chunks.
// pcm_in:      array of int16 PCM samples
// num_samples: number of samples
// results:     output array, one byte per frequency: 1 = tone present, 0 = absent
//              caller must allocate at least num_freqs bytes
// Returns number of complete blocks processed.
CVSD_API int tone_detector_feed(ToneDetector* det,
                                const int16_t* pcm_in, int num_samples,
                                uint8_t* results);

// Same as tone_detector_feed but also returns magnitudes for each frequency.
// magnitudes:  output array of doubles, one per frequency (caller allocates num_freqs)
//              holds the magnitude from the last completed block.
CVSD_API int tone_detector_feed_mag(ToneDetector* det,
                                    const int16_t* pcm_in, int num_samples,
                                    uint8_t* results, double* magnitudes);

// Reset detector state
CVSD_API void tone_detector_reset(ToneDetector* det);

// Free a tone detector
CVSD_API void tone_detector_destroy(ToneDetector* det);

#endif // CVSD_CODEC_H
