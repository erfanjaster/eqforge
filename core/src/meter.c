/* EQForge native core - metering implementation.
 *
 * K-weighting follows ITU-R BS.1770-4 (generalized bilinear designs so any
 * sample rate works). Momentary loudness uses a 400 ms sliding window of
 * per-sample channel-weighted mean-square values.
 *
 * SPDX-License-Identifier: MIT
 */
#include "eqforge/meter.h"

#include <math.h>
#include <stdlib.h>
#include <string.h>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

/* channel weight per BS.1770: L/R = 1.0, additional (surround) = 1.41 */
static inline float channel_weight(uint32_t c)
{
    return c < 2 ? 1.0f : 1.41f;
}

int eqf_meter_k_weight_design(float sample_rate, float coeffs_out[2][5])
{
    /* Stage 1: head-model high shelf (+4 dB @ high freq) */
    {
        double G = 3.999843853973347;
        double Q = 0.7071752369554196;
        double fc = 1681.974450955533;
        double K = tan(M_PI * fc / (double)sample_rate);
        double Vh = pow(10.0, G / 20.0);
        double Vb = pow(Vh, 0.4996865530411197);
        double a0 = 1.0 + K / Q + K * K;
        coeffs_out[0][0] = (float)((Vh + Vb * K / Q + K * K) / a0);
        coeffs_out[0][1] = (float)(2.0 * (K * K - Vh) / a0);
        coeffs_out[0][2] = (float)((Vh - Vb * K / Q + K * K) / a0);
        coeffs_out[0][3] = (float)(2.0 * (K * K - 1.0) / a0);
        coeffs_out[0][4] = (float)((1.0 - K / Q + K * K) / a0);
    }
    /* Stage 2: RLB high-pass */
    {
        double Q = 0.5003270373238773;
        double fc = 38.13547087602444;
        double K = tan(M_PI * fc / (double)sample_rate);
        double a0 = 1.0 + K / Q + K * K;
        coeffs_out[1][0] = (float)(1.0 / a0);
        coeffs_out[1][1] = (float)(-2.0 / a0);
        coeffs_out[1][2] = (float)(1.0 / a0);
        coeffs_out[1][3] = (float)(2.0 * (K * K - 1.0) / a0);
        coeffs_out[1][4] = (float)((1.0 - K / Q + K * K) / a0);
    }
    return EQF_OK;
}

int eqf_meter_init(eqf_meter *m, uint32_t channels, uint32_t sample_rate)
{
    memset(m, 0, sizeof(*m));
    m->channels = channels ? channels : 2;
    if (m->channels > EQF_MAX_CHANNELS)
        m->channels = EQF_MAX_CHANNELS;
    m->sample_rate = sample_rate ? sample_rate : 48000;
    m->window_len = (uint32_t)((double)m->sample_rate * EQF_METER_MOMENTARY_MS / 1000.0);
    if (m->window_len == 0)
        m->window_len = 1;
    m->window = (float *)calloc(m->window_len, sizeof(float));
    if (!m->window)
        return EQF_ERR_NO_MEMORY;
    m->allocated = true;
    eqf_meter_k_weight_design((float)m->sample_rate, m->k_coeffs);
    eqf_meter_reset(m);
    return EQF_OK;
}

void eqf_meter_free(eqf_meter *m)
{
    if (!m)
        return;
    free(m->window);
    m->window = NULL;
    m->allocated = false;
}

void eqf_meter_reset(eqf_meter *m)
{
    memset(m->peak, 0, sizeof(m->peak));
    memset(m->rms, 0, sizeof(m->rms));
    memset(m->true_peak, 0, sizeof(m->true_peak));
    memset(m->k_state, 0, sizeof(m->k_state));
    memset(m->tp_state, 0, sizeof(m->tp_state));
    if (m->window)
        memset(m->window, 0, sizeof(float) * m->window_len);
    m->window_sum = 0.0;
    m->window_pos = 0;
    m->window_filled = 0;
    m->momentary_lufs = -70.0f;
    m->clipped = false;
}

/* 4x-oversampled true peak estimation over one block (single channel).
 * state: 8 floats of sample history (newest at index 0 after the call). */
float eqf_true_peak_detect(const float *buf, uint32_t frames, float state[8])
{
    /* 32-tap Hann-windowed sinc polyphase, same design as the limiter */
    const int TAPS = 32;
    double h[TAPS];
    const double fc = 0.125; /* fs/2 in the 4x prototype domain */
    for (int n = 0; n < TAPS; n++) {
        double x = n - (TAPS - 1) / 2.0;
        double s = (fabs(x) < 1e-9) ? 2.0 * fc : sin(2.0 * M_PI * fc * x) / (M_PI * x);
        double w = 0.5 - 0.5 * cos(2.0 * M_PI * n / (TAPS - 1));
        h[n] = 4.0 * s * w;
    }
    float phases[4][8];
    for (int p = 0; p < 4; p++)
        for (int k = 0; k < 8; k++)
            phases[p][k] = (float)h[4 * k + p];

    float hist[8];
    memcpy(hist, state, sizeof(hist));
    float peak = 0.0f;

    for (uint32_t i = 0; i < frames; i++) {
        float x = buf[i];
        hist[7] = hist[6]; hist[6] = hist[5]; hist[5] = hist[4]; hist[4] = hist[3];
        hist[3] = hist[2]; hist[2] = hist[1]; hist[1] = hist[0]; hist[0] = x;
        /* hist[k] == x[i-k]; y[4i+p] = sum_k hist[k] * proto[4k+p] */
        float a = fabsf(x);
        if (a > peak)
            peak = a;
        for (int p = 1; p < 4; p++) {
            float acc = 0.0f;
            for (int k = 0; k < 8; k++)
                acc += phases[p][k] * hist[k];
            a = fabsf(acc);
            if (a > peak)
                peak = a;
        }
    }
    memcpy(state, hist, sizeof(hist));
    return peak;
}

void eqf_meter_update(eqf_meter *m, const float *const *chans, uint32_t frames)
{
    if (!m || !m->allocated || frames == 0)
        return;

    /* per-block peak / RMS / clipping / true peak */
    for (uint32_t c = 0; c < m->channels; c++) {
        const float *x = chans[c];
        float peak = 0.0f, sumsq = 0.0f;
        for (uint32_t i = 0; i < frames; i++) {
            float a = fabsf(x[i]);
            if (a > peak)
                peak = a;
            sumsq += x[i] * x[i];
        }
        m->peak[c] = peak;
        m->rms[c] = sqrtf(sumsq / (float)frames);
        if (peak >= 1.0f)
            m->clipped = true;
        float tp = eqf_true_peak_detect(x, frames, m->tp_state[c]);
        if (tp > m->true_peak[c])
            m->true_peak[c] = tp;
    }

    /* K-weighted momentary window */
    float b00 = m->k_coeffs[0][0], b01 = m->k_coeffs[0][1], b02 = m->k_coeffs[0][2];
    float a01 = m->k_coeffs[0][3], a02 = m->k_coeffs[0][4];
    float b10 = m->k_coeffs[1][0], b11 = m->k_coeffs[1][1], b12 = m->k_coeffs[1][2];
    float a11 = m->k_coeffs[1][3], a12 = m->k_coeffs[1][4];

    uint32_t pos = m->window_pos;
    for (uint32_t i = 0; i < frames; i++) {
        float acc = 0.0f;
        for (uint32_t c = 0; c < m->channels; c++) {
            float *st0 = m->k_state[c][0];
            float *st1 = m->k_state[c][1];
            float x = chans[c][i];
            /* stage 1 */
            float y1 = b00 * x + st0[0];
            st0[0] = b01 * x - a01 * y1 + st0[1];
            st0[1] = b02 * x - a02 * y1;
            /* stage 2 */
            float y2 = b10 * y1 + st1[0];
            st1[0] = b11 * y1 - a11 * y2 + st1[1];
            st1[1] = b12 * y1 - a12 * y2;
            acc += channel_weight(c) * y2 * y2;
        }
        /* maintain running window sum: drop oldest, add newest (O(1)) */
        if (m->window_filled == m->window_len)
            m->window_sum -= (double)m->window[pos];
        m->window_sum += (double)acc;
        m->window[pos] = acc;
        pos++;
        if (pos >= m->window_len)
            pos = 0;
    }
    m->window_pos = pos;
    if (m->window_filled < m->window_len) {
        m->window_filled += frames;
        if (m->window_filled > m->window_len)
            m->window_filled = m->window_len;
    }

    if (m->window_filled > 0 && m->window_sum > 1e-12) {
        double mean = m->window_sum / (double)m->window_filled;
        m->momentary_lufs = (float)(-0.691 + 10.0 * log10(mean));
    } else {
        m->momentary_lufs = -70.0f;
    }
}
