/* EQForge native core - level metering.
 *
 * Per-block peak / RMS, 4x-oversampled true-peak estimation and BS.1770
 * K-weighted momentary loudness (400 ms blocks, per the ITU-R gate-less
 * "momentary" definition). Full gated LUFS/LRA measurement lives in the
 * analysis layer (offline); this meter is for live UI readouts.
 *
 * SPDX-License-Identifier: MIT
 */
#ifndef EQFORGE_METER_H
#define EQFORGE_METER_H

#include <stdbool.h>
#include <stdint.h>

#include "types.h"

#ifdef __cplusplus
extern "C" {
#endif

#define EQF_METER_MOMENTARY_MS 400

typedef struct {
    uint32_t channels;
    uint32_t sample_rate;
    /* live values */
    float peak[EQF_MAX_CHANNELS];       /* last block sample peak, linear */
    float rms[EQF_MAX_CHANNELS];        /* last block RMS, linear */
    float true_peak[EQF_MAX_CHANNELS];  /* held max true peak since reset, linear */
    float momentary_lufs;               /* K-weighted, last 400 ms window */
    /* K-weighting filter state (2 stages: pre-filter shelf + RLB highpass) */
    float k_state[EQF_MAX_CHANNELS][2][2];
    float k_coeffs[2][5];               /* b0 b1 b2 a1 a2 per stage */
    /* momentary window ring of per-sample squared K-weighted sums */
    float *window;                      /* allocated: window_len entries */
    double window_sum;                  /* running sum of window entries */
    uint32_t window_len;
    uint32_t window_pos;
    uint32_t window_filled;
    /* true-peak oversampler state */
    float tp_state[EQF_MAX_CHANNELS][8];
    bool clipped;                       /* set when |sample| >= 1.0 observed */
    bool allocated;
} eqf_meter;

int eqf_meter_init(eqf_meter *m, uint32_t channels, uint32_t sample_rate);
void eqf_meter_free(eqf_meter *m);
void eqf_meter_reset(eqf_meter *m);
/* Update from planar input; peaks/rms refer to this block, momentary to the
 * trailing 400 ms window, true_peak is a held maximum. */
void eqf_meter_update(eqf_meter *m, const float *const *chans, uint32_t frames);

/* K-weighting biquad design helpers (exposed for tests and parity checks). */
int eqf_meter_k_weight_design(float sample_rate, float coeffs_out[2][5]);

/* 4x oversampled true-peak of a buffer (single channel). state: 8 floats. */
float eqf_true_peak_detect(const float *buf, uint32_t frames, float state[8]);

#ifdef __cplusplus
}
#endif

#endif /* EQFORGE_METER_H */
