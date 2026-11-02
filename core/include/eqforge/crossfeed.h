/* EQForge native core - Bauer stereophonic-to-binaural crossfeed.
 *
 * Feeds an attenuated, low-passed copy of each channel into the opposite
 * ear, reducing the unnatural "inside the head" lateralization of headphone
 * playback.
 *
 *     L' = 0.5 * (L + feed * H(f) * R)
 *     R' = 0.5 * (R + feed * H(f) * L)
 *
 * H(f) is a single 2nd-order low-pass at fc_hz and feed = 10^(level_db/20).
 * The 0.5 makes the network loudness preserving for a centred source, which
 * is what lets a listener leave it enabled permanently. Note that the feed
 * path is H(f) itself, not a cascade: cascading two sections would square the
 * response and, with the previous non-normalised summing, produced a +3.5 dB
 * bass boost instead of a smooth crossfeed.
 *
 * SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-EQForge-Commercial
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
    eqf_biquad lp[2][2];    /* [direction][stage]; only stage 0 is in use */
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
