/* EQForge native core - dynamics implementation (compressor + limiter).
 *
 * SPDX-License-Identifier: MIT
 */
#include "eqforge/dynamics.h"

#include <math.h>
#include <string.h>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

static inline float db2lin(float db) { return powf(10.0f, db / 20.0f); }

static inline float lin2db(float x)
{
    return x > 1e-9f ? 20.0f * log10f(x) : -180.0f;
}

static inline float coeff_for(double ms, double sample_rate)
{
    if (ms <= 0.0)
        return 1.0f;
    return (float)exp(-1.0 / (ms * 0.001 * sample_rate));
}

/* ==================== compressor ==================== */

void eqf_compressor_config_default(eqf_compressor_config *c)
{
    c->threshold_db = -18.0;
    c->ratio = 3.0;
    c->knee_db = 6.0;
    c->attack_ms = 10.0;
    c->release_ms = 120.0;
    c->makeup_db = 0.0;
    c->stereo_link = true;
    c->rms_detect = false;
    c->rms_window_ms = 20.0;
}

void eqf_compressor_init(eqf_compressor *cp, uint32_t channels, uint32_t sample_rate)
{
    memset(cp, 0, sizeof(*cp));
    cp->channels = channels ? channels : 2;
    cp->sample_rate = sample_rate ? sample_rate : 48000;
    eqf_compressor_config_default(&cp->cfg);
    eqf_compressor_configure(cp, &cp->cfg);
    cp->gain_db = 0.0f;
    cp->active = false;
}

int eqf_compressor_configure(eqf_compressor *cp, const eqf_compressor_config *cfg)
{
    if (!cp || !cfg)
        return EQF_ERR_INVALID_ARG;
    if (cfg->ratio < 1.0)
        return EQF_ERR_INVALID_ARG;
    cp->cfg = *cfg;
    cp->atk_coeff = coeff_for(cfg->attack_ms, cp->sample_rate);
    cp->rel_coeff = coeff_for(cfg->release_ms, cp->sample_rate);
    cp->rms_coeff = coeff_for(cfg->rms_window_ms, cp->sample_rate);
    cp->makeup = db2lin((float)cfg->makeup_db);
    return EQF_OK;
}

void eqf_compressor_reset(eqf_compressor *cp)
{
    for (uint32_t c = 0; c < EQF_MAX_CHANNELS; c++)
        cp->env_ch[c] = 0.0f;
    cp->gain_db = 0.0f;
    cp->last_gr_db = 0.0f;
}

void eqf_compressor_set_active(eqf_compressor *cp, bool active)
{
    if (cp->active == active)
        return;
    cp->active = active;
    eqf_compressor_reset(cp);
}

static inline float compute_gain_db(const eqf_compressor *cp, float env_db)
{
    float T = (float)cp->cfg.threshold_db;
    float R = (float)cp->cfg.ratio;
    float W = (float)cp->cfg.knee_db;
    float over = env_db - T;
    float gr; /* gain reduction, positive dB */
    if (W > 0.0f && over > -W * 0.5f && over < W * 0.5f) {
        float x = over + W * 0.5f;
        gr = (1.0f - 1.0f / R) * (x * x) / (2.0f * W);
    } else if (over >= W * 0.5f) {
        gr = (1.0f - 1.0f / R) * over;
    } else {
        gr = 0.0f;
    }
    return -gr;
}

void eqf_compressor_process(eqf_compressor *cp, float *const *chans, uint32_t frames)
{
    if (!cp->active)
        return;

    float atk = cp->atk_coeff, rel = cp->rel_coeff, rmsc = cp->rms_coeff;

    for (uint32_t i = 0; i < frames; i++) {
        float det = 0.0f;
        for (uint32_t c = 0; c < cp->channels; c++) {
            float x = chans[c][i];
            float v;
            if (cp->cfg.rms_detect) {
                float *e = &cp->env_ch[c];
                *e = rmsc * (*e) + (1.0f - rmsc) * (x * x);
                v = sqrtf(*e);
            } else {
                v = fabsf(x);
            }
            if (v > det)
                det = v;
        }
        if (det < 1e-9f)
            det = 1e-9f;
        float det_db = lin2db(det);
        float target_db = compute_gain_db(cp, det_db);

        /* smooth gain in the dB domain: attack toward louder (more
         * reduction), release back to unity */
        float coeff = (target_db < cp->gain_db) ? atk : rel;
        cp->gain_db = coeff * cp->gain_db + (1.0f - coeff) * target_db;

        float g = db2lin(cp->gain_db) * cp->makeup;
        for (uint32_t c = 0; c < cp->channels; c++)
            chans[c][i] *= g;
    }
    cp->last_gr_db = cp->gain_db; /* negative when reducing */
}

float eqf_compressor_gain_reduction_db(const eqf_compressor *cp)
{
    return cp->active ? -cp->last_gr_db : 0.0f; /* positive dB of reduction */
}

/* ==================== limiter ==================== */

void eqf_limiter_config_default(eqf_limiter_config *c)
{
    c->ceiling_db = -1.0;
    c->attack_ms = 5.0;
    c->release_ms = 60.0;
    c->lookahead_ms = 1.5;
    c->knee_db = 3.0;
}

/* 4x oversampling polyphase interpolation filter: 32-tap Hann-windowed sinc
 * prototype split into 4 phases of 8 taps. Used to estimate inter-sample
 * (true) peaks. */
static void design_os_filter(float phases[4][8])
{
    const int TAPS = 32;
    double h[TAPS];
    const double fc = 0.125; /* fs/2 in the 4x prototype domain */
    for (int n = 0; n < TAPS; n++) {
        double x = n - (TAPS - 1) / 2.0;
        double s = (fabs(x) < 1e-9) ? 2.0 * fc : sin(2.0 * M_PI * fc * x) / (M_PI * x);
        double w = 0.5 - 0.5 * cos(2.0 * M_PI * n / (TAPS - 1));
        h[n] = 4.0 * s * w; /* x4 to compensate zero-stuffing energy */
    }
    for (int p = 0; p < 4; p++)
        for (int k = 0; k < 8; k++)
            phases[p][k] = (float)h[4 * k + p];
}

void eqf_limiter_init(eqf_limiter *lim, uint32_t channels, uint32_t sample_rate)
{
    memset(lim, 0, sizeof(*lim));
    lim->channels = channels ? channels : 2;
    lim->sample_rate = sample_rate ? sample_rate : 48000;
    eqf_limiter_config_default(&lim->cfg);
    lim->gain = 1.0f;
    lim->active = false;
    lim->configured = false;
    lim->os_phases_valid = false;
}

int eqf_limiter_configure(eqf_limiter *lim, const eqf_limiter_config *cfg)
{
    if (!lim || !cfg)
        return EQF_ERR_INVALID_ARG;
    if (cfg->ceiling_db > 0.0 || cfg->ceiling_db < -60.0)
        return EQF_ERR_INVALID_ARG;
    lim->cfg = *cfg;
    lim->ceiling = db2lin((float)cfg->ceiling_db);
    lim->knee = db2lin((float)(cfg->ceiling_db - cfg->knee_db));
    lim->atk_coeff = coeff_for(cfg->attack_ms, lim->sample_rate);
    lim->rel_coeff = coeff_for(cfg->release_ms, lim->sample_rate);
    lim->lookahead = (uint32_t)(cfg->lookahead_ms * 0.001 * (double)lim->sample_rate);
    if (lim->lookahead > 1024)
        lim->lookahead = 1024;
    lim->delay_len = lim->lookahead + 1;
    if (!lim->os_phases_valid) {
        design_os_filter(lim->os_phases);
        lim->os_phases_valid = true;
    }
    lim->configured = true;
    return EQF_OK;
}

void eqf_limiter_reset(eqf_limiter *lim)
{
    for (uint32_t c = 0; c < EQF_MAX_CHANNELS; c++) {
        memset(lim->delay[c], 0, sizeof(lim->delay[c]));
        memset(lim->os_state[c], 0, sizeof(lim->os_state[c]));
    }
    lim->write_pos = 0;
    lim->gain = 1.0f;
    lim->last_peak = 0.0f;
    lim->last_gain_db = 0.0f;
}

void eqf_limiter_set_active(eqf_limiter *lim, bool active)
{
    if (lim->active == active)
        return;
    lim->active = active;
    eqf_limiter_reset(lim);
}

uint32_t eqf_limiter_latency(const eqf_limiter *lim)
{
    return lim->active ? lim->lookahead : 0;
}

float eqf_limiter_gain_db(const eqf_limiter *lim)
{
    return lin2db(lim->gain);
}

float eqf_limiter_true_peak(const eqf_limiter *lim)
{
    return lim->last_peak;
}

void eqf_limiter_process(eqf_limiter *lim, float *const *chans, uint32_t frames)
{
    if (!lim->active || !lim->configured)
        return;

    const float ceiling = lim->ceiling;
    const float knee = lim->knee;
    const float atk = lim->atk_coeff, rel = lim->rel_coeff;
    const uint32_t dl = lim->delay_len;
    float peak_seen = lim->last_peak;

    for (uint32_t i = 0; i < frames; i++) {
        /* --- estimate the true peak of the incoming (look-ahead) sample --- */
        float det = 0.0f;
        for (uint32_t c = 0; c < lim->channels; c++) {
            float x = chans[c][i];
            /* shift 8-sample history; hist[0] == newest */
            float *h = lim->os_state[c];
            h[7] = h[6]; h[6] = h[5]; h[5] = h[4]; h[4] = h[3];
            h[3] = h[2]; h[2] = h[1]; h[1] = h[0]; h[0] = x;
            /* evaluate the three interpolated points between the two most
             * recent input samples */
            for (int p = 1; p < 4; p++) {
                float acc = 0.0f;
                for (int k = 0; k < 8; k++)
                    acc += lim->os_phases[p][k] * h[k];
                float a = fabsf(acc);
                if (a > det)
                    det = a;
            }
            float a = fabsf(x);
            if (a > det)
                det = a;
        }
        if (det > peak_seen)
            peak_seen = det;

        /* --- desired gain for the look-ahead sample --- */
        float want = 1.0f;
        if (det > knee) {
            if (det <= ceiling) {
                float t = (det - knee) / (ceiling - knee + 1e-9f);
                want = 1.0f - t * t * (1.0f - knee / (ceiling + 1e-9f)) * 0.5f;
            } else {
                want = ceiling / det;
            }
        }

        /* --- smooth gain: fast attack, slower release --- */
        float coeff = (want < lim->gain) ? atk : rel;
        lim->gain = coeff * lim->gain + (1.0f - coeff) * want;

        /* --- apply gain to the delayed (look-ahead compensated) sample --- */
        uint32_t rp = (lim->write_pos + 1) % dl; /* oldest stored sample */
        float g = lim->gain;
        for (uint32_t c = 0; c < lim->channels; c++) {
            float out = lim->delay[c][rp] * g;
            lim->delay[c][lim->write_pos] = chans[c][i];
            /* final safety clamp for extreme transients the smoother misses */
            if (out > ceiling)
                out = ceiling;
            else if (out < -ceiling)
                out = -ceiling;
            chans[c][i] = out;
        }
        lim->write_pos = rp;
    }

    lim->last_peak = peak_seen;
    lim->last_gain_db = lin2db(lim->gain);
}
