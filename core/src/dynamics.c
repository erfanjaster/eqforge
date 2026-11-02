/* EQForge native core - dynamics implementation (compressor + limiter).
 *
 * SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-EQForge-Commercial
 */
#include "eqforge/dynamics.h"

#include <math.h>
#include <string.h>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

#define EQF_DB_FLOOR (-160.0f) /* treated as silence by the log domain */

static inline float db2lin(float db) { return powf(10.0f, db / 20.0f); }

static inline float lin2db(float x)
{
    return x > 1e-9f ? 20.0f * log10f(x) : -180.0f;
}

/* Make a signal non-finite-safe: NaN -> 0, +-Inf -> +-1. Used at the few
 * places where a corrupted sample could latch into filter state and persist
 * for the life of the process. */
static inline float sanitize_sample(float x)
{
    if (!isfinite(x))
        return isinf(x) ? (x > 0.0f ? 1.0f : -1.0f) : 0.0f;
    return x;
}

static inline float flush_denormal(float x)
{
    /* Denormals (< FLT_MIN ~ 1.18e-38) are flushed so they cannot crawl in
     * feedback state; 1e-20 is comfortably above FLT_MIN and far below any
     * audible signal (-400 dBFS). */
    return fabsf(x) < 1e-20f ? 0.0f : x;
}

static inline float coeff_for(double ms, double sample_rate)
{
    if (ms <= 0.0)
        return 0.0f; /* 0 == "instant": gain equals its target immediately */
    double c = exp(-1.0 / (ms * 0.001 * sample_rate));
    if (c < 0.0)
        c = 0.0;
    if (c > 1.0)
        c = 1.0;
    return (float)c;
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
    if (!(cfg->ratio >= 1.0) || !isfinite(cfg->ratio))
        return EQF_ERR_INVALID_ARG;
    if (!isfinite(cfg->threshold_db) || !isfinite(cfg->attack_ms) ||
        !isfinite(cfg->release_ms) || cfg->attack_ms < 0.0 ||
        cfg->release_ms < 0.0 || cfg->knee_db < 0.0)
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
            float x = sanitize_sample(chans[c][i]);
            float v;
            if (cp->cfg.rms_detect) {
                float *e = &cp->env_ch[c];
                *e = flush_denormal(rmsc * (*e) + (1.0f - rmsc) * (x * x));
                v = sqrtf(*e);
            } else {
                v = fabsf(x);
            }
            if (v > det)
                det = v;
        }
        if (!(det >= 1e-9f))
            det = 1e-9f;
        float det_db = lin2db(det);
        float target_db = compute_gain_db(cp, det_db);

        /* Smooth gain in the dB domain: attack toward louder (more
         * reduction), release back to unity. */
        float coeff = (target_db < cp->gain_db) ? atk : rel;
        cp->gain_db = coeff * cp->gain_db + (1.0f - coeff) * target_db;
        cp->gain_db = flush_denormal(cp->gain_db);

        float g = db2lin(cp->gain_db) * cp->makeup;
        for (uint32_t c = 0; c < cp->channels; c++)
            chans[c][i] = sanitize_sample(chans[c][i]) * g;
    }
    cp->last_gr_db = cp->gain_db; /* negative when reducing */
}

float eqf_compressor_gain_reduction_db(const eqf_compressor *cp)
{
    return cp->active ? -cp->last_gr_db : 0.0f; /* positive dB of reduction */
}

/* ==================== true-peak oversampler ==================== */

/* 4x polyphase interpolator: 32-tap Hann-windowed sinc prototype split into
 * four 8-tap phases. Phase 0 is the original sample grid; phases 1..3 are the
 * in-between estimates. Used by both the limiter detector and the meter. */
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

/* ==================== look-ahead limiter ==================== */

void eqf_limiter_config_default(eqf_limiter_config *c)
{
    c->ceiling_db = -1.0;
    c->attack_ms = 5.0;
    c->release_ms = 60.0;
    c->lookahead_ms = 1.5;
    c->knee_db = 3.0;
}

void eqf_limiter_init(eqf_limiter *lim, uint32_t channels, uint32_t sample_rate)
{
    memset(lim, 0, sizeof(*lim));
    lim->channels = channels ? channels : 2;
    if (lim->channels > EQF_MAX_CHANNELS)
        lim->channels = EQF_MAX_CHANNELS;
    lim->sample_rate = sample_rate ? sample_rate : 48000;
    eqf_limiter_config_default(&lim->cfg);
    lim->gain = 1.0f;
    lim->active = false;
    lim->configured = false;
    lim->os_phases_valid = false;
    lim->latency = 0;
}

/* Latency (frames) a configuration would introduce. 0 when the limiter is
 * disabled, because the stage is fully bypassed and delays nothing. */
static uint32_t lookahead_frames(double lookahead_ms, uint32_t sample_rate)
{
    if (lookahead_ms <= 0.0)
        return 0;
    double frames = lookahead_ms * 0.001 * (double)sample_rate;
    if (frames < 1.0)
        frames = 1.0;
    if (frames > (double)EQF_LIMITER_MAX_LOOKAHEAD)
        frames = (double)EQF_LIMITER_MAX_LOOKAHEAD;
    return (uint32_t)(frames + 0.5);
}

int eqf_limiter_configure(eqf_limiter *lim, const eqf_limiter_config *cfg)
{
    if (!lim || !cfg)
        return EQF_ERR_INVALID_ARG;
    if (!isfinite(cfg->ceiling_db) || cfg->ceiling_db > 0.0 ||
        cfg->ceiling_db < -60.0)
        return EQF_ERR_INVALID_ARG;
    if (!isfinite(cfg->attack_ms) || !isfinite(cfg->release_ms) ||
        !isfinite(cfg->lookahead_ms) || cfg->attack_ms < 0.0 ||
        cfg->release_ms <= 0.0 || cfg->lookahead_ms < 0.0)
        return EQF_ERR_INVALID_ARG;
    if (!isfinite(cfg->knee_db) || cfg->knee_db < 0.0)
        return EQF_ERR_INVALID_ARG;

    lim->cfg = *cfg;
    lim->ceiling = db2lin((float)cfg->ceiling_db);
    lim->knee = db2lin((float)(cfg->ceiling_db - cfg->knee_db));
    if (!(lim->knee > 0.0f))
        lim->knee = lim->ceiling;
    lim->atk_coeff = coeff_for(cfg->attack_ms, lim->sample_rate);
    lim->rel_coeff = coeff_for(cfg->release_ms, lim->sample_rate);
    lim->lookahead = lookahead_frames(cfg->lookahead_ms, lim->sample_rate);
    lim->delay_len = lim->lookahead + 1;
    {
        /* Ring size must be a power of two so the hot loop can mask instead of
         * modulo; the mask is size-1, not size (a mask of exactly 128 aliases
         * every index onto slot 0/128 and silences the output). */
        uint32_t p2 = 1;
        while (p2 <= lim->delay_len) /* strict: window < ring size */
            p2 <<= 1;
        if (p2 > EQF_LIMITER_DELAY_LEN)
            p2 = EQF_LIMITER_DELAY_LEN;
        lim->delay_mask = p2 - 1;
    }
    lim->latency = lim->lookahead;
    /* With look-ahead the descriptor guarantees the gain is already low when
     * the peak reaches the output, so descent must be instantaneous; without
     * look-ahead there is nothing to hide the jump and the attack time
     * constant is the only protection left. */
    lim->eff_atk_coeff = lim->lookahead > 0 ? 0.0f : lim->atk_coeff;
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
    memset(lim->want_hist, 0, sizeof(lim->want_hist));
    memset(lim->dq, 0, sizeof(lim->dq));
    lim->dq_head = 0;
    lim->dq_count = 0;
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
    /* The stage is bypassed when inactive: it must not pretend to delay. */
    return lim->active ? lim->latency : 0;
}

/* Latency for a prospective configuration, independent of active state.
 * Chain/host code uses this for compensation bookkeeping. */
uint32_t eqf_limiter_configured_latency(const eqf_limiter *lim)
{
    return lim ? lim->latency : 0;
}

float eqf_limiter_gain_db(const eqf_limiter *lim)
{
    return lin2db(lim->gain);
}

float eqf_limiter_true_peak(const eqf_limiter *lim)
{
    return lim->last_peak;
}

/* Target gain (dB, <= 0) for a detected peak, soft knee in the dB domain.
 *
 * Let over = det_db - ceiling_db and W = knee width in dB. Then
 *
 *   over <= -W : gain = 0                     (below the knee)
 *   over >=  0 : gain = -over                 (exactly ceiling/det linear)
 *   otherwise  : gain = -over * ((over+W)/W)^2 (quadratic blend)
 *
 * The blend is C1-continuous at both ends: the derivative is 0 at over = -W
 * and 1 at over = 0, matching the flat and linear regions. Above the knee the
 * requested reduction is therefore applied *exactly*, so the delayed sample
 * lands on the ceiling instead of relying on a hard clamp to fix the error.
 */
static inline float limiter_want_gain_db(float det_db, float ceiling_db,
                                         float knee_width_db)
{
    float over = det_db - ceiling_db;
    if (!(ceiling_db < 0.0f))
        return 0.0f;
    if (over <= -knee_width_db)
        return 0.0f;
    if (over >= 0.0f)
        return -over;
    float t = (over + knee_width_db) / knee_width_db;
    return -(over * t * t);
}

void eqf_limiter_process(eqf_limiter *lim, float *const *chans, uint32_t frames)
{
    if (!lim->active || !lim->configured)
        return;

    const float ceiling = lim->ceiling;
    const float ceiling_db = (float)lim->cfg.ceiling_db;
    const float knee_db = (float)lim->cfg.knee_db;
    const float rel = lim->rel_coeff;
    const float atk = lim->eff_atk_coeff;
    const uint32_t mask = lim->delay_mask;
    const uint32_t size = mask + 1u;
    const uint32_t la = lim->lookahead;
    const uint32_t window = la + 1u;   /* strict subset of `size` */
    uint32_t wp = lim->write_pos & mask;
    uint32_t head = lim->dq_head;      /* deque front (oldest candidate) */
    uint32_t count = lim->dq_count;    /* entries in the deque */
    float peak_seen = lim->last_peak;
    float gain_db = lin2db(lim->gain);

    for (uint32_t i = 0; i < frames; i++) {
        /* --- 0. true-peak estimate of the look-ahead (newest) sample --- */
        float det = 0.0f;
        for (uint32_t c = 0; c < lim->channels; c++) {
            float x = sanitize_sample(chans[c][i]);
            chans[c][i] = x;
            float *h = lim->os_state[c];
            h[7] = h[6]; h[6] = h[5]; h[5] = h[4]; h[4] = h[3];
            h[3] = h[2]; h[2] = h[1]; h[1] = h[0]; h[0] = x;
            float a = fabsf(x);
            if (a > det)
                det = a;
            for (int p = 1; p < 4; p++) {
                float acc = 0.0f;
                for (int k = 0; k < 8; k++)
                    acc += lim->os_phases[p][k] * h[k];
                a = fabsf(acc);
                if (a > det)
                    det = a;
            }
        }
        if (det > peak_seen)
            peak_seen = det;

        float want_db = limiter_want_gain_db(lin2db(det), ceiling_db, knee_db);
        if (!(want_db <= 0.0f))
            want_db = 0.0f;
        lim->want_hist[wp] = want_db;

        /* --- 1. sliding-window minimum of want_db over the last `window`
         * samples, kept as a monotonic (increasing) deque of ring positions.
         * Because the window is strictly smaller than the ring, an entry
         * leaves the window only when `window` newer samples have arrived. */
        while (count > 0) {
            uint32_t front = lim->dq[head];
            uint32_t age = (wp - front) & mask;
            if (age >= window) {
                head = (head + 1u) % size;
                count--;
            } else {
                break;
            }
        }
        while (count > 0) {
            uint32_t back = lim->dq[(head + count - 1u) % size];
            if (lim->want_hist[back] >= want_db)
                count--;
            else
                break;
        }
        lim->dq[(head + count) % size] = wp;
        count++;
        if (count > size) {           /* unreachable, but keeps it safe */
            head = (head + 1u) % size;
            count = size;
        }

        float min_want = lim->want_hist[lim->dq[head]];

        /* --- 2. gain movement: instant descent, time-constant release --- */
        if (min_want < gain_db)
            gain_db = atk > 0.0f ? (atk * gain_db + (1.0f - atk) * min_want)
                                : min_want;
        else
            gain_db = rel * gain_db + (1.0f - rel) * min_want;
        if (!isfinite(gain_db))
            gain_db = 0.0f;
        if (gain_db > 0.0f)
            gain_db = 0.0f;
        if (gain_db < EQF_DB_FLOOR)
            gain_db = EQF_DB_FLOOR;

        /* --- 3. apply to the oldest stored sample (true look-ahead) --- */
        float g = db2lin(gain_db);
        uint32_t rp = (wp - la) & mask;
        for (uint32_t c = 0; c < lim->channels; c++) {
            float out = lim->delay[c][rp] * g;
            lim->delay[c][wp] = chans[c][i];
            /* safety net: only reachable on non-finite/absurd input */
            if (!(out <= ceiling))
                out = ceiling;
            else if (!(out >= -ceiling))
                out = -ceiling;
            chans[c][i] = out;
        }
        wp = (wp + 1u) & mask;
    }

    lim->write_pos = wp;
    lim->dq_head = head;
    lim->dq_count = count;
    lim->gain = db2lin(gain_db);
    lim->last_peak = peak_seen;
    lim->last_gain_db = gain_db;
}

/* Exposed so the meter can share the exact same interpolation kernel. */
void eqf_true_peak_kernel(float phases[4][8])
{
    design_os_filter(phases);
}
