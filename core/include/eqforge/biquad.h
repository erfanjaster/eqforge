/* EQForge native core - biquad filter design and processing.
 *
 * Design follows Robert Bristow-Johnson's Audio EQ Cookbook. Processing is
 * Direct Form II Transposed, which is numerically well behaved in float and
 * needs two state variables per channel.
 *
 * Parameter changes are smoothed: the caller programs *target* coefficients
 * and the processor ramps the live coefficients toward them, eliminating
 * zipper noise for real-time parameter automation.
 *
 * SPDX-License-Identifier: MIT
 */
#ifndef EQFORGE_BIQUAD_H
#define EQFORGE_BIQUAD_H

#include <stdbool.h>

#include "types.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    float b0, b1, b2, a1, a2; /* normalized (a0 == 1) */
} eqf_biquad_coeffs;

typedef struct {
    eqf_biquad_coeffs current; /* smoothed coefficients used by processing */
    eqf_biquad_coeffs target;  /* where we are ramping to */
    float ramp_step[5];        /* per-coefficient increment per sample */
    int ramp_left;             /* samples remaining in the ramp */
    float z1, z2;              /* DF2T state */
    bool bypassed;
} eqf_biquad;

/* Q semantics: for shelves, `use_q == false` interprets the shape parameter
 * as a shelf slope S (cookbook style). For peak/pass/notch filters Q is the
 * classic quality factor. */
int eqf_biquad_design(eqf_biquad_coeffs *c, enum eqf_filter_type type,
                      double freq_hz, double gain_db, double q_or_slope,
                      bool use_q, double sample_rate);

void eqf_biquad_init(eqf_biquad *f);
void eqf_biquad_reset(eqf_biquad *f);

/* Program new target coefficients; they are reached linearly over
 * ramp_samples (0 = immediate). */
void eqf_biquad_set_target(eqf_biquad *f, const eqf_biquad_coeffs *target,
                           int ramp_samples);

/* Process `n` frames in place (single channel). */
void eqf_biquad_process(eqf_biquad *f, float *buf, int n);

/* Evaluate the magnitude response of a coefficient set at f_hz (linear). */
double eqf_biquad_magnitude(const eqf_biquad_coeffs *c, double f_hz,
                            double sample_rate);

/* Evaluate total magnitude response (product) of a filter bank. */
double eqf_bank_magnitude(const eqf_biquad_coeffs *coeffs, int count,
                          double f_hz, double sample_rate);

#ifdef __cplusplus
}
#endif

#endif /* EQFORGE_BIQUAD_H */
