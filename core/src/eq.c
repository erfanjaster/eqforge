/* EQForge native core - multi-band parametric EQ implementation.
 *
 * SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-EQForge-Commercial
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
    for (uint32_t c = 0; c < EQF_MAX_CHANNELS; c++) {
        eq->bands_per_channel[c] = 0;
        for (int b = 0; b < EQF_MAX_BANDS; b++)
            eqf_biquad_reset(&eq->filters[c][b]);
    }
    eq->active = false;
}

void eqf_eq_reset(eqf_eq *eq)
{
    for (uint32_t c = 0; c < EQF_MAX_CHANNELS; c++)
        for (int b = 0; b < EQF_MAX_BANDS; b++)
            eqf_biquad_reset(&eq->filters[c][b]);
}

void eqf_eq_set_sample_rate(eqf_eq *eq, uint32_t sample_rate)
{
    if (sample_rate == 0 || sample_rate == eq->sample_rate)
        return;
    /* The coefficient set in `design` cannot be re-derived without the band
     * specs (type/gain/Q), so callers must reload the band table after a rate
     * change. Until they do, the EQ is bypassed rather than left running with
     * coefficients designed for a different rate (which detunes every band). */
    eq->sample_rate = sample_rate;
    eq->active = false;
    for (uint32_t c = 0; c < EQF_MAX_CHANNELS; c++)
        eq->bands_per_channel[c] = 0;
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
    int n_active = 0;
    for (int i = 0; i < count; i++) {
        if (!bands[i].enabled)
            continue;
        n_active++;
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

    /* Retire filters that will no longer be used: clearing their state keeps a
     * later re-add of a band from starting on stale history (a removed band used
     * to ring for ever the moment its state was reused). */
    for (uint32_t c = 0; c < eq->channels; c++) {
        for (int b = per_ch[c]; b < EQF_MAX_BANDS; b++) {
            eqf_biquad_reset(&eq->filters[c][b]);
            eq->filters[c][b].current.b0 = 1.0f;
            eq->filters[c][b].current.b1 = 0.0f;
            eq->filters[c][b].current.b2 = 0.0f;
            eq->filters[c][b].current.a1 = 0.0f;
            eq->filters[c][b].current.a2 = 0.0f;
            eq->filters[c][b].target = eq->filters[c][b].current;
            eq->filters[c][b].ramp_left = 0;
        }
    }

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

    eq->active = (n_active > 0);
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
