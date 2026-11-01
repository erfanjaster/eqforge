/* EQForge native core - multi-band parametric equalizer.
 *
 * Per-channel filter banks. Bands with channel == "all" are duplicated into
 * every channel bank with independent state, so the common stereo case has
 * identical coefficients but per-channel processing.
 *
 * SPDX-License-Identifier: MIT
 */
#ifndef EQFORGE_EQ_H
#define EQFORGE_EQ_H

#include <stdbool.h>
#include <stdint.h>

#include "biquad.h"
#include "types.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    enum eqf_filter_type type;
    double freq;
    double gain_db;
    double q;          /* quality factor, or slope when type is a shelf and use_q==false */
    bool use_q;
    bool enabled;
    int channel;       /* -1 == all channels */
} eqf_band_spec;

typedef struct {
    uint32_t channels;
    uint32_t sample_rate;
    int bands_per_channel[EQF_MAX_CHANNELS];
    eqf_biquad filters[EQF_MAX_CHANNELS][EQF_MAX_BANDS];
    eqf_biquad_coeffs design[EQF_MAX_CHANNELS][EQF_MAX_BANDS]; /* last design, for introspection */
    bool active;
} eqf_eq;

void eqf_eq_init(eqf_eq *eq, uint32_t channels, uint32_t sample_rate);
void eqf_eq_clear(eqf_eq *eq);
void eqf_eq_reset(eqf_eq *eq);
void eqf_eq_set_sample_rate(eqf_eq *eq, uint32_t sample_rate);

/* Load a band table. ramp_ms controls how quickly the new design is reached
 * from the current coefficients (0 = immediately). */
int eqf_eq_load(eqf_eq *eq, const eqf_band_spec *bands, int count,
                double ramp_ms);

/* Process planar audio: chans[c] points to `frames` floats. */
void eqf_eq_process(eqf_eq *eq, float *const *chans, uint32_t frames);

/* Combined magnitude response of channel `ch` at f_hz (linear amplitude). */
double eqf_eq_response(const eqf_eq *eq, int ch, double f_hz);

#ifdef __cplusplus
}
#endif

#endif /* EQFORGE_EQ_H */
