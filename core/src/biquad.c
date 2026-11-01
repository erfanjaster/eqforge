/* EQForge native core - biquad implementation.
 *
 * Filter design: Robert Bristow-Johnson's Audio EQ Cookbook formulas.
 * Processing: Direct Form II Transposed with per-sample coefficient ramping.
 *
 * SPDX-License-Identifier: MIT
 */
#include "eqforge/biquad.h"

#include <math.h>
#include <string.h>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

#define TWO_PI (2.0 * M_PI)

int eqf_biquad_design(eqf_biquad_coeffs *c, enum eqf_filter_type type,
                      double freq_hz, double gain_db, double q_or_slope,
                      bool use_q, double sample_rate)
{
    if (!c || sample_rate <= 0.0)
        return EQF_ERR_INVALID_ARG;
    if (freq_hz <= 0.0)
        freq_hz = 1.0;
    if (freq_hz >= sample_rate * 0.5)
        freq_hz = sample_rate * 0.5 - 1.0;
    if (freq_hz <= 0.0)
        freq_hz = 1.0;

    double A = pow(10.0, gain_db / 40.0);
    double w0 = TWO_PI * freq_hz / sample_rate;
    double cw0 = cos(w0);
    double sw0 = sin(w0);
    double alpha = 0.0;

    if (!use_q && q_or_slope > 0.0 && q_or_slope <= 1.0) {
        /* shelf slope parameter S */
        alpha = sw0 / 2.0 * sqrt((A + 1.0 / A) * (1.0 / q_or_slope - 1.0) + 2.0);
    } else {
        double q = q_or_slope;
        if (q <= 0.0001)
            q = 0.0001;
        alpha = sw0 / (2.0 * q);
    }

    double b0 = 1.0, b1 = 0.0, b2 = 0.0, a0 = 1.0, a1 = 0.0, a2 = 0.0;
    double sqrtA, two_sqrtA_alpha;

    switch (type) {
    case EQF_FILTER_PEAK:
        b0 = 1.0 + alpha * A;
        b1 = -2.0 * cw0;
        b2 = 1.0 - alpha * A;
        a0 = 1.0 + alpha / A;
        a1 = -2.0 * cw0;
        a2 = 1.0 - alpha / A;
        break;
    case EQF_FILTER_LOWPASS:
        b0 = (1.0 - cw0) / 2.0;
        b1 = 1.0 - cw0;
        b2 = (1.0 - cw0) / 2.0;
        a0 = 1.0 + alpha;
        a1 = -2.0 * cw0;
        a2 = 1.0 - alpha;
        break;
    case EQF_FILTER_HIGHPASS:
        b0 = (1.0 + cw0) / 2.0;
        b1 = -(1.0 + cw0);
        b2 = (1.0 + cw0) / 2.0;
        a0 = 1.0 + alpha;
        a1 = -2.0 * cw0;
        a2 = 1.0 - alpha;
        break;
    case EQF_FILTER_BANDPASS:
        b0 = alpha;
        b1 = 0.0;
        b2 = -alpha;
        a0 = 1.0 + alpha;
        a1 = -2.0 * cw0;
        a2 = 1.0 - alpha;
        break;
    case EQF_FILTER_NOTCH:
        b0 = 1.0;
        b1 = -2.0 * cw0;
        b2 = 1.0;
        a0 = 1.0 + alpha;
        a1 = -2.0 * cw0;
        a2 = 1.0 - alpha;
        break;
    case EQF_FILTER_ALLPASS:
        b0 = 1.0 - alpha;
        b1 = -2.0 * cw0;
        b2 = 1.0 + alpha;
        a0 = 1.0 + alpha;
        a1 = -2.0 * cw0;
        a2 = 1.0 - alpha;
        break;
    case EQF_FILTER_LOW_SHELF:
    case EQF_FILTER_LOWSHELF_12DB:
        sqrtA = sqrt(A);
        two_sqrtA_alpha = 2.0 * sqrtA * alpha;
        b0 = A * ((A + 1.0) - (A - 1.0) * cw0 + two_sqrtA_alpha);
        b1 = 2.0 * A * ((A - 1.0) - (A + 1.0) * cw0);
        b2 = A * ((A + 1.0) - (A - 1.0) * cw0 - two_sqrtA_alpha);
        a0 = (A + 1.0) + (A - 1.0) * cw0 + two_sqrtA_alpha;
        a1 = -2.0 * ((A - 1.0) + (A + 1.0) * cw0);
        a2 = (A + 1.0) + (A - 1.0) * cw0 - two_sqrtA_alpha;
        break;
    case EQF_FILTER_HIGH_SHELF:
    case EQF_FILTER_HIGHSHELF_12DB:
        sqrtA = sqrt(A);
        two_sqrtA_alpha = 2.0 * sqrtA * alpha;
        b0 = A * ((A + 1.0) + (A - 1.0) * cw0 + two_sqrtA_alpha);
        b1 = -2.0 * A * ((A - 1.0) + (A + 1.0) * cw0);
        b2 = A * ((A + 1.0) + (A - 1.0) * cw0 - two_sqrtA_alpha);
        a0 = (A + 1.0) - (A - 1.0) * cw0 + two_sqrtA_alpha;
        a1 = 2.0 * ((A - 1.0) - (A + 1.0) * cw0);
        a2 = (A + 1.0) - (A - 1.0) * cw0 - two_sqrtA_alpha;
        break;
    default:
        return EQF_ERR_INVALID_ARG;
    }

    if (fabs(a0) < 1e-12)
        return EQF_ERR_INVALID_ARG;

    c->b0 = (float)(b0 / a0);
    c->b1 = (float)(b1 / a0);
    c->b2 = (float)(b2 / a0);
    c->a1 = (float)(a1 / a0);
    c->a2 = (float)(a2 / a0);
    return EQF_OK;
}

void eqf_biquad_init(eqf_biquad *f)
{
    memset(f, 0, sizeof(*f));
    /* unity passthrough */
    f->current.b0 = f->target.b0 = 1.0f;
}

void eqf_biquad_reset(eqf_biquad *f)
{
    f->z1 = 0.0f;
    f->z2 = 0.0f;
}

void eqf_biquad_set_target(eqf_biquad *f, const eqf_biquad_coeffs *target,
                           int ramp_samples)
{
    f->target = *target;
    if (ramp_samples <= 0) {
        f->current = f->target;
        f->ramp_left = 0;
        return;
    }
    float inv = 1.0f / (float)ramp_samples;
    f->ramp_step[0] = (f->target.b0 - f->current.b0) * inv;
    f->ramp_step[1] = (f->target.b1 - f->current.b1) * inv;
    f->ramp_step[2] = (f->target.b2 - f->current.b2) * inv;
    f->ramp_step[3] = (f->target.a1 - f->current.a1) * inv;
    f->ramp_step[4] = (f->target.a2 - f->current.a2) * inv;
    f->ramp_left = ramp_samples;
}

void eqf_biquad_process(eqf_biquad *f, float *buf, int n)
{
    if (f->bypassed)
        return;
    float b0 = f->current.b0, b1 = f->current.b1, b2 = f->current.b2;
    float a1 = f->current.a1, a2 = f->current.a2;
    float z1 = f->z1, z2 = f->z2;
    int ramp = f->ramp_left;

    if (ramp <= 0) {
        for (int i = 0; i < n; i++) {
            float x = buf[i];
            float y = b0 * x + z1;
            z1 = b1 * x - a1 * y + z2;
            z2 = b2 * x - a2 * y;
            buf[i] = y;
        }
    } else {
        float sb0 = f->ramp_step[0], sb1 = f->ramp_step[1], sb2 = f->ramp_step[2];
        float sa1 = f->ramp_step[3], sa2 = f->ramp_step[4];
        int i = 0;
        for (; i < n && ramp > 0; i++, ramp--) {
            b0 += sb0; b1 += sb1; b2 += sb2; a1 += sa1; a2 += sa2;
            float x = buf[i];
            float y = b0 * x + z1;
            z1 = b1 * x - a1 * y + z2;
            z2 = b2 * x - a2 * y;
            buf[i] = y;
        }
        f->ramp_left = 0;
        f->current = f->target;
        b0 = f->current.b0; b1 = f->current.b1; b2 = f->current.b2;
        a1 = f->current.a1; a2 = f->current.a2;
        for (; i < n; i++) {
            float x = buf[i];
            float y = b0 * x + z1;
            z1 = b1 * x - a1 * y + z2;
            z2 = b2 * x - a2 * y;
            buf[i] = y;
        }
    }

    f->z1 = z1;
    f->z2 = z2;

    /* Safety net: denormals can crawl on some x86 units; flush them. */
    if (fabsf(z1) < 1e-20f) f->z1 = 0.0f;
    if (fabsf(z2) < 1e-20f) f->z2 = 0.0f;
}

double eqf_biquad_magnitude(const eqf_biquad_coeffs *c, double f_hz,
                            double sample_rate)
{
    double w = TWO_PI * f_hz / sample_rate;
    double cw = cos(w), cw2 = cos(2.0 * w);
    double sw = sin(w), sw2 = sin(2.0 * w);

    double b0 = c->b0, b1 = c->b1, b2 = c->b2;
    double a1 = c->a1, a2 = c->a2;

    /* H(e^jw) = (b0 + b1 e^-jw + b2 e^-2jw) / (1 + a1 e^-jw + a2 e^-2jw) */
    double br = b0 + b1 * cw + b2 * cw2;
    double bi = -(b1 * sw + b2 * sw2);
    double ar = 1.0 + a1 * cw + a2 * cw2;
    double ai = -(a1 * sw + a2 * sw2);

    double num = br * br + bi * bi;
    double den = ar * ar + ai * ai;
    if (den < 1e-30)
        return 0.0;
    return sqrt(num / den);
}

double eqf_bank_magnitude(const eqf_biquad_coeffs *coeffs, int count,
                          double f_hz, double sample_rate)
{
    double mag = 1.0;
    for (int i = 0; i < count; i++)
        mag *= eqf_biquad_magnitude(&coeffs[i], f_hz, sample_rate);
    return mag;
}
