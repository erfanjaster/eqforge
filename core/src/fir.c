/* EQForge native core - FIR convolver implementation.
 *
 * Direct-form convolution over a per-channel history ring. O(taps) per
 * sample; suitable for headphone-correction IRs up to a few thousand taps.
 *
 * SPDX-License-Identifier: MIT
 */
#include "eqforge/fir.h"

#include <math.h>
#include <string.h>

void eqf_convolver_init(eqf_convolver *cv, uint32_t channels)
{
    memset(cv, 0, sizeof(*cv));
    cv->channels = channels ? channels : 2;
    cv->gain = 1.0f;
    cv->active = false;
}

int eqf_convolver_set_ir(eqf_convolver *cv, int channel, const float *taps, uint32_t count)
{
    if (!cv || channel < 0 || (uint32_t)channel >= cv->channels)
        return EQF_ERR_INVALID_ARG;
    if (count > EQF_MAX_FIR_TAPS)
        return EQF_ERR_UNSUPPORTED;
    if (count > 0 && !taps)
        return EQF_ERR_INVALID_ARG;
    memcpy(cv->ir[channel], taps, count * sizeof(float));
    cv->taps[channel] = count;
    return EQF_OK;
}

int eqf_convolver_set_ir_all(eqf_convolver *cv, const float *taps, uint32_t count)
{
    for (uint32_t c = 0; c < cv->channels; c++) {
        int r = eqf_convolver_set_ir(cv, (int)c, taps, count);
        if (r != EQF_OK)
            return r;
    }
    return EQF_OK;
}

void eqf_convolver_set_gain(eqf_convolver *cv, float gain_linear)
{
    cv->gain = gain_linear;
}

void eqf_convolver_reset(eqf_convolver *cv)
{
    memset(cv->history, 0, sizeof(cv->history));
    cv->history_pos = 0;
}

void eqf_convolver_set_active(eqf_convolver *cv, bool active)
{
    if (cv->active == active)
        return;
    cv->active = active;
    if (active)
        eqf_convolver_reset(cv);
}

uint32_t eqf_convolver_max_taps(const eqf_convolver *cv)
{
    uint32_t m = 0;
    for (uint32_t c = 0; c < cv->channels; c++)
        if (cv->taps[c] > m)
            m = cv->taps[c];
    return m;
}

void eqf_convolver_process(eqf_convolver *cv, float *const *chans, uint32_t frames)
{
    if (!cv->active)
        return;
    float gain = cv->gain;
    uint32_t pos = cv->history_pos;

    for (uint32_t c = 0; c < cv->channels; c++) {
        uint32_t nt = cv->taps[c];
        float *hist = cv->history[c];
        float *out = chans[c];
        const float *ir = cv->ir[c];
        uint32_t p = pos;

        if (nt == 0)
            continue;

        for (uint32_t i = 0; i < frames; i++) {
            hist[p] = out[i];
            float acc = 0.0f;
            uint32_t q = p;
            for (uint32_t k = 0; k < nt; k++) {
                acc += ir[k] * hist[q];
                q = (q == 0) ? (EQF_MAX_FIR_TAPS - 1) : (q - 1);
            }
            out[i] = acc * gain;
            p = (p + 1) % EQF_MAX_FIR_TAPS;
        }
    }
    /* All channels advance identically. */
    for (uint32_t i = 0; i < frames; i++)
        pos = (pos + 1) % EQF_MAX_FIR_TAPS;
    cv->history_pos = pos;
}
