/* EQForge native core - multi-band parametric EQ implementation.
 *
 * SPDX-License-Identifier: MIT
 */
#include "eqforge/eq.h"

#include <math.h>
#include <string.h>

void eqf_eq_init(eqf_eq *eq, uint32_t channels, uint32_t sample_rate)
{
    memset(eq, 0, sizeof(*eq));
    if (channels == 0 || channels > EQF_MAX_CHANNELS)
        channels = 2;
    eq->channels = channels;
    eq->sample_rate = sample_rate ? sample_rate : 48000;
    for (uint32_t c = 0; c < EQF_MAX_CHANNELS; c++)
        for (int b = 0; b < EQF_MAX_BANDS; b++)
            eqf_biquad_init(&eq->filters[c][b]);
    eq->active = false;
}

void eqf_eq_clear(eqf_eq *eq)
{
    for (uint32_t c = 0; c < eq->channels; c++)
        eq->bands_per_channel[c] = 0;
    eq->active = false;
}

void eqf_eq_reset(eqf_eq *eq)
{
    for (uint32_t c = 0; c < eq->channels; c++)
        for (int b = 0; b < eq->bands_per_channel[c]; b++)
            eqf_biquad_reset(&eq->filters[c][b]);
}

void eqf_eq_set_sample_rate(eqf_eq *eq, uint32_t sample_rate)
{
    if (sample_rate == 0 || sample_rate == eq->sample_rate)
        return;
    /* Re-design all existing bands for the new rate, preserving the specs
     * stored implicitly in `design` is not enough (we need type/gain/q), so
     * callers must reload bands after a rate change; we simply invalidate. */
    eq->sample_rate = sample_rate;
}

int eqf_eq_load(eqf_eq *eq, const eqf_band_spec *bands, int count,
                double ramp_ms)
{
    if (!eq || (count > 0 && !bands))
        return EQF_ERR_INVALID_ARG;
    if (count > EQF_MAX_BANDS * (int)eq->channels)
        return EQF_ERR_INVALID_ARG;

    /* Count per-channel allocations first. */
    int per_ch[EQF_MAX_CHANNELS] = { 0 };
    for (int i = 0; i < count; i++) {
        if (!bands[i].enabled)
            continue;
        int ch = bands[i].channel;
        if (ch < 0) {
            for (uint32_t c = 0; c < eq->channels; c++)
                per_ch[c]++;
        } else if ((uint32_t)ch < eq->channels) {
            per_ch[ch]++;
        } else {
            return EQF_ERR_INVALID_ARG;
        }
    }
    for (uint32_t c = 0; c < eq->channels; c++)
        if (per_ch[c] > EQF_MAX_BANDS)
            return EQF_ERR_INVALID_ARG;

    int ramp = (int)(ramp_ms * 0.001 * (double)eq->sample_rate);

    int idx[EQF_MAX_CHANNELS] = { 0 };
    for (int i = 0; i < count; i++) {
        if (!bands[i].enabled)
            continue;
        eqf_biquad_coeffs coef;
        if (eqf_biquad_design(&coef, bands[i].type, bands[i].freq,
                              bands[i].gain_db, bands[i].q, bands[i].use_q,
                              (double)eq->sample_rate) != EQF_OK)
            continue; /* skip undesignable band rather than fail everything */

        if (bands[i].channel < 0) {
            for (uint32_t c = 0; c < eq->channels; c++) {
                int b = idx[c]++;
                eq->design[c][b] = coef;
                eqf_biquad_set_target(&eq->filters[c][b], &coef, ramp);
            }
        } else {
            uint32_t c = (uint32_t)bands[i].channel;
            int b = idx[c]++;
            eq->design[c][b] = coef;
            eqf_biquad_set_target(&eq->filters[c][b], &coef, ramp);
        }
    }
    for (uint32_t c = 0; c < eq->channels; c++)
        eq->bands_per_channel[c] = per_ch[c];

    /* Retire filters that are no longer used: set to unity so leftover state
     * doesn't ring. They are not processed anyway (bands_per_channel). */
    eq->active = (count > 0);
    return EQF_OK;
}

void eqf_eq_process(eqf_eq *eq, float *const *chans, uint32_t frames)
{
    if (!eq->active)
        return;
    for (uint32_t c = 0; c < eq->channels; c++) {
        int nb = eq->bands_per_channel[c];
        float *buf = chans[c];
        for (int b = 0; b < nb; b++)
            eqf_biquad_process(&eq->filters[c][b], buf, (int)frames);
    }
}

double eqf_eq_response(const eqf_eq *eq, int ch, double f_hz)
{
    if (!eq || ch < 0 || (uint32_t)ch >= eq->channels)
        return 1.0;
    int nb = eq->bands_per_channel[ch];
    if (nb == 0)
        return 1.0;
    return eqf_bank_magnitude(eq->design[ch], nb, f_hz, (double)eq->sample_rate);
}
