/* EQForge native core - FIR convolver for impulse-response correction.
 *
 * Time-domain direct-form convolution with per-channel IRs, bounded to
 * EQF_MAX_FIR_TAPS. Intended for headphone correction FIRs (a few thousand
 * taps). Latency is zero; CPU cost is O(taps). A future C++ port can swap in
 * a partitioned FFT convolver behind the same interface.
 *
 * SPDX-License-Identifier: MIT
 */
#ifndef EQFORGE_FIR_H
#define EQFORGE_FIR_H

#include <stdbool.h>
#include <stdint.h>

#include "types.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    uint32_t channels;
    uint32_t taps[EQF_MAX_CHANNELS];
    float ir[EQF_MAX_CHANNELS][EQF_MAX_FIR_TAPS];
    float history[EQF_MAX_CHANNELS][EQF_MAX_FIR_TAPS];
    uint32_t history_pos;
    float gain;      /* additional linear gain applied with the IR */
    bool active;
} eqf_convolver;

void eqf_convolver_init(eqf_convolver *cv, uint32_t channels);
int eqf_convolver_set_ir(eqf_convolver *cv, int channel, const float *taps, uint32_t count);
int eqf_convolver_set_ir_all(eqf_convolver *cv, const float *taps, uint32_t count);
void eqf_convolver_set_gain(eqf_convolver *cv, float gain_linear);
void eqf_convolver_reset(eqf_convolver *cv);
void eqf_convolver_set_active(eqf_convolver *cv, bool active);
void eqf_convolver_process(eqf_convolver *cv, float *const *chans, uint32_t frames);
uint32_t eqf_convolver_max_taps(const eqf_convolver *cv);

#ifdef __cplusplus
}
#endif

#endif /* EQFORGE_FIR_H */
