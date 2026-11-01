/* EQForge native core - dynamics processing.
 *
 * eqf_compressor: feed-forward compressor with peak or RMS detection, soft
 * knee, optional stereo link and makeup gain.
 *
 * eqf_limiter: look-ahead peak limiter with true-peak detection (4x
 * oversampled absolute peaks via polyphase interpolation), soft knee near the
 * ceiling, and per-sample gain smoothing (attack/release). Latency introduced
 * equals the look-ahead depth and is reported via eqf_limiter_latency().
 *
 * Both are RT-safe: no allocation or syscalls inside process().
 *
 * SPDX-License-Identifier: MIT
 */
#ifndef EQFORGE_DYNAMICS_H
#define EQFORGE_DYNAMICS_H

#include <stdbool.h>
#include <stdint.h>

#include "types.h"

#ifdef __cplusplus
extern "C" {
#endif

/* ---------- compressor ---------- */

typedef struct {
    double threshold_db;
    double ratio;         /* >= 1 */
    double knee_db;       /* soft knee width */
    double attack_ms;
    double release_ms;
    double makeup_db;
    bool stereo_link;
    bool rms_detect;
    double rms_window_ms;
} eqf_compressor_config;

void eqf_compressor_config_default(eqf_compressor_config *c);

typedef struct {
    eqf_compressor_config cfg;
    uint32_t channels;
    uint32_t sample_rate;
    float env_ch[EQF_MAX_CHANNELS];
    float atk_coeff, rel_coeff, rms_coeff;
    float makeup;
    float gain_db;        /* smoothed gain in dB (0 = unity) */
    bool active;
    float last_gr_db;     /* introspection: current gain reduction */
} eqf_compressor;

void eqf_compressor_init(eqf_compressor *cp, uint32_t channels, uint32_t sample_rate);
int eqf_compressor_configure(eqf_compressor *cp, const eqf_compressor_config *cfg);
void eqf_compressor_reset(eqf_compressor *cp);
void eqf_compressor_set_active(eqf_compressor *cp, bool active);
void eqf_compressor_process(eqf_compressor *cp, float *const *chans, uint32_t frames);
float eqf_compressor_gain_reduction_db(const eqf_compressor *cp);

/* ---------- look-ahead limiter ---------- */

typedef struct {
    double ceiling_db;    /* e.g. -1.0 dBTP */
    double attack_ms;     /* gain ramp attack */
    double release_ms;    /* gain ramp release */
    double lookahead_ms;  /* 0 disables look-ahead (still limiting, may clip transients) */
    double knee_db;       /* soft knee below ceiling */
} eqf_limiter_config;

void eqf_limiter_config_default(eqf_limiter_config *c);

typedef struct {
    eqf_limiter_config cfg;
    uint32_t channels;
    uint32_t sample_rate;
    uint32_t lookahead;   /* frames */
    float ceiling;        /* linear */
    float knee;           /* linear width */
    float atk_coeff, rel_coeff;
    float gain;           /* current gain (linear, <= 1) */
    float delay[EQF_MAX_CHANNELS][EQF_MAX_BLOCK + 1024]; /* ring buffers */
    uint32_t write_pos;
    uint32_t delay_len;
    /* 4x oversampled true-peak detection */
    float os_state[EQF_MAX_CHANNELS][8];
    float os_phases[4][8];      /* cached polyphase interpolation filter */
    bool os_phases_valid;
    bool active;
    float last_peak;      /* introspection: last detected true peak (linear) */
    float last_gain_db;   /* introspection */
    bool configured;
} eqf_limiter;

void eqf_limiter_init(eqf_limiter *lim, uint32_t channels, uint32_t sample_rate);
int eqf_limiter_configure(eqf_limiter *lim, const eqf_limiter_config *cfg);
void eqf_limiter_reset(eqf_limiter *lim);
void eqf_limiter_set_active(eqf_limiter *lim, bool active);
void eqf_limiter_process(eqf_limiter *lim, float *const *chans, uint32_t frames);
uint32_t eqf_limiter_latency(const eqf_limiter *lim); /* frames of delay introduced */
float eqf_limiter_gain_db(const eqf_limiter *lim);
float eqf_limiter_true_peak(const eqf_limiter *lim);

#ifdef __cplusplus
}
#endif

#endif /* EQFORGE_DYNAMICS_H */
