/* EQForge native core - Bauer stereophonic-to-binaural crossfeed.
 *
 * Feeds an attenuated, low-passed copy of each channel into the opposite
 * ear, reducing the unnatural "inside the head" lateralization of headphone
 * playback. Two biquad low-pass sections per direction approximate the
 * classic Meier-style crossfeed network.
 *
 * SPDX-License-Identifier: MIT
 */
#ifndef EQFORGE_CROSSFEED_H
#define EQFORGE_CROSSFEED_H

#include <stdbool.h>
#include <stdint.h>

#include "biquad.h"
#include "types.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    bool enabled;
    double level_db;   /* contralateral feed level, e.g. -6 */
    double fc_hz;      /* crossfeed corner, e.g. 650-900 Hz */
} eqf_crossfeed_config;

void eqf_crossfeed_config_default(eqf_crossfeed_config *c);

typedef struct {
    eqf_crossfeed_config cfg;
    uint32_t sample_rate;
    float feed;             /* linear crossfeed gain */
    eqf_biquad lp[2][2];    /* [direction][stage] */
    float scratch[EQF_MAX_BLOCK];
    float scratch2[EQF_MAX_BLOCK];
    bool active;
} eqf_crossfeed;

void eqf_crossfeed_init(eqf_crossfeed *xf, uint32_t sample_rate);
int eqf_crossfeed_configure(eqf_crossfeed *xf, const eqf_crossfeed_config *cfg);
void eqf_crossfeed_reset(eqf_crossfeed *xf);
void eqf_crossfeed_set_active(eqf_crossfeed *xf, bool active);
/* Only acts when exactly two channels are provided (L and R buffers). */
void eqf_crossfeed_process(eqf_crossfeed *xf, float *l, float *r, uint32_t frames);

#ifdef __cplusplus
}
#endif

#endif /* EQFORGE_CROSSFEED_H */
