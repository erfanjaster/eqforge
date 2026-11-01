/* EQForge native core - crossfeed implementation.
 *
 * SPDX-License-Identifier: MIT
 */
#include "eqforge/crossfeed.h"

#include <math.h>
#include <string.h>

void eqf_crossfeed_config_default(eqf_crossfeed_config *c)
{
    c->enabled = false;
    c->level_db = -6.0;
    c->fc_hz = 700.0;
}

void eqf_crossfeed_init(eqf_crossfeed *xf, uint32_t sample_rate)
{
    memset(xf, 0, sizeof(*xf));
    xf->sample_rate = sample_rate ? sample_rate : 48000;
    eqf_crossfeed_config_default(&xf->cfg);
    for (int d = 0; d < 2; d++)
        for (int s = 0; s < 2; s++)
            eqf_biquad_init(&xf->lp[d][s]);
    xf->active = false;
}

int eqf_crossfeed_configure(eqf_crossfeed *xf, const eqf_crossfeed_config *cfg)
{
    if (!xf || !cfg)
        return EQF_ERR_INVALID_ARG;
    if (cfg->fc_hz < 50.0 || cfg->fc_hz > 4000.0)
        return EQF_ERR_INVALID_ARG;
    if (cfg->level_db > 0.0 || cfg->level_db < -30.0)
        return EQF_ERR_INVALID_ARG;
    xf->cfg = *cfg;
    xf->feed = (float)pow(10.0, cfg->level_db / 20.0);

    /* Two cascaded Butterworth low-passes (Q = 0.707 each stage gives a
     * 4th-order Linkwitz-Riley-ish roll-off when squared; here each stage is
     * Q=0.5412/1.3066 pair approximation via simple Q=0.7 each). */
    eqf_biquad_coeffs c;
    if (eqf_biquad_design(&c, EQF_FILTER_LOWPASS, cfg->fc_hz, 0.0, 0.7071,
                          true, (double)xf->sample_rate) != EQF_OK)
        return EQF_ERR_INVALID_ARG;
    for (int d = 0; d < 2; d++)
        for (int s = 0; s < 2; s++)
            eqf_biquad_set_target(&xf->lp[d][s], &c, 0);
    return EQF_OK;
}

void eqf_crossfeed_reset(eqf_crossfeed *xf)
{
    for (int d = 0; d < 2; d++)
        for (int s = 0; s < 2; s++)
            eqf_biquad_reset(&xf->lp[d][s]);
}

void eqf_crossfeed_set_active(eqf_crossfeed *xf, bool active)
{
    if (xf->active == active)
        return;
    xf->active = active;
    eqf_crossfeed_reset(xf);
}

void eqf_crossfeed_process(eqf_crossfeed *xf, float *l, float *r, uint32_t frames)
{
    if (!xf->active || !l || !r || frames == 0)
        return;

    /* Process in scratch-sized chunks so callers may pass any length. */
    uint32_t off = 0;
    while (off < frames) {
        uint32_t n = frames - off;
        if (n > EQF_MAX_BLOCK)
            n = EQF_MAX_BLOCK;
        float *lp = l + off;
        float *rp = r + off;

        /* Snapshot both channels first so the two cross paths never see each
         * other's contribution (no intra-block feedback). */
        float *sl = xf->scratch;
        float *sr = xf->scratch2;
        memcpy(sl, lp, n * sizeof(float));
        memcpy(sr, rp, n * sizeof(float));
        float feed = xf->feed;

        /* left -> right */
        eqf_biquad_process(&xf->lp[0][0], sl, (int)n);
        eqf_biquad_process(&xf->lp[0][1], sl, (int)n);
        for (uint32_t i = 0; i < n; i++)
            rp[i] += feed * sl[i];

        /* right -> left */
        eqf_biquad_process(&xf->lp[1][0], sr, (int)n);
        eqf_biquad_process(&xf->lp[1][1], sr, (int)n);
        for (uint32_t i = 0; i < n; i++)
            lp[i] += feed * sr[i];

        off += n;
    }
}
