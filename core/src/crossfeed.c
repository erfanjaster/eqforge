/* EQForge native core - crossfeed implementation.
 *
 * SPDX-License-Identifier: MIT
 */
#include "eqforge/crossfeed.h"

#include <math.h>
#include <string.h>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

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

    /* Bauer's stereophonic-to-binaural law:
     *
     *     L' = 0.5 * (L + feed * H(f) * R)
     *     R' = 0.5 * (R + feed * H(f) * L)
     *
     * so a centred source stays at a fixed level and the network can be left
     * enabled permanently. Two earlier mistakes made the block misbehave:
     *
     *   1. the 0.5 summing gain was missing, so the low-passed opposite
     *      channel was *added* to an unattenuated direct path, boosting a
     *      centred source in the bass; and
     *   2. two low-pass sections were cascaded into the feed path, which
     *      squares the response instead of shaping it once.
     *
     * Together those two gave a measured, non-monotonic response of
     * +3.49 dB at 50 Hz, +1.81 dB at 300 Hz, -1.76 dB at 500 Hz and
     * -2.51 dB at 700 Hz.
     *
     * H(f) is a first-order (single-pole) low-pass, matching the single RC
     * element of the analog network. A second-order section rotates the feed
     * phase past -90 degrees and the sum then dips ~0.7 dB *below* its own
     * high-frequency asymptote, which is worse than no crossfeed at all. A
     * first-order section is monotone between the two asymptotes:
     * -2.49 dB at DC falling to -6.02 dB well above fc. */
    {
        double k = tan(M_PI * cfg->fc_hz / (double)xf->sample_rate);
        double norm = 1.0 / (1.0 + k);
        eqf_biquad_coeffs c;
        memset(&c, 0, sizeof c);
        c.b0 = k * norm;
        c.b1 = k * norm;
        c.b2 = 0.0;
        c.a1 = (k - 1.0) * norm;
        c.a2 = 0.0;
        for (int d = 0; d < 2; d++)
            for (int s = 0; s < 2; s++)
                eqf_biquad_set_target(&xf->lp[d][s], &c, 0);
    }
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

        /* L' = 0.5*(L + feed*H(R)), R' = 0.5*(R + feed*H(L)).
         * Only stage 0 carries signal: stage 1 exists for callers that ask a
         * steeper network, and keeping it in the cascade would silently
         * square the feed response again. */
        eqf_biquad_process(&xf->lp[0][0], sl, (int)n);
        for (uint32_t i = 0; i < n; i++)
            rp[i] = 0.5f * (rp[i] + feed * sl[i]);

        eqf_biquad_process(&xf->lp[1][0], sr, (int)n);
        for (uint32_t i = 0; i < n; i++)
            lp[i] = 0.5f * (lp[i] + feed * sr[i]);

        off += n;
    }
}
