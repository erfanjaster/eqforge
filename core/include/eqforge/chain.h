/* EQForge native core - processing chain.
 *
 * Fixed, sensible stage order:
 *
 *   input gain -> EQ -> crossfeed -> convolver -> compressor -> limiter -> output gain
 *
 * The chain is configured from a resolved "dsp config" JSON document (see
 * docs/profile-format.md). Configuration may happen from a non-audio thread;
 * audio-visible state changes are applied with a short fade to avoid clicks.
 *
 * SPDX-License-Identifier: MIT
 */
#ifndef EQFORGE_CHAIN_H
#define EQFORGE_CHAIN_H

#include <stdbool.h>
#include <stdint.h>

#include "crossfeed.h"
#include "dynamics.h"
#include "eq.h"
#include "fir.h"
#include "meter.h"
#include "types.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct eqf_chain eqf_chain;

/* Snapshot of interesting chain state, safe to read from any thread after
 * eqf_chain_process() returns. */
typedef struct {
    float in_peak[EQF_MAX_CHANNELS];
    float in_rms[EQF_MAX_CHANNELS];
    float out_peak[EQF_MAX_CHANNELS];
    float out_rms[EQF_MAX_CHANNELS];
    float out_true_peak[EQF_MAX_CHANNELS];
    float momentary_lufs;
    float limiter_gain_db;
    float limiter_true_peak;
    float compressor_gr_db;
    bool in_clipped;
    bool out_clipped;
    uint32_t latency_frames;
    uint32_t channels;
} eqf_chain_stats;

/* Lifecycle -------------------------------------------------------------- */

/* max_block: largest frame count that will be passed to process(). */
eqf_chain *eqf_chain_new(uint32_t channels, uint32_t sample_rate, uint32_t max_block);
void eqf_chain_free(eqf_chain *c);

/* Configuration ---------------------------------------------------------- */

/* Configure from a resolved dsp-config JSON document. The chain keeps
 * running; changes are applied with a short mute fade to avoid clicks. */
int eqf_chain_configure_json(eqf_chain *c, const char *json, size_t len);
int eqf_chain_configure_file(eqf_chain *c, const char *path);

/* Sample-rate change (e.g. PipeWire graph switch): re-designs all filters,
 * resets delay lines, keeps the current profile. */
int eqf_chain_set_sample_rate(eqf_chain *c, uint32_t sample_rate);

/* Channel count change: re-allocates state. */
int eqf_chain_set_channels(eqf_chain *c, uint32_t channels);

/* Runtime controls -------------------------------------------------------- */

void eqf_chain_set_bypass(eqf_chain *c, bool bypass);
bool eqf_chain_get_bypass(const eqf_chain *c);

/* When enabled (default), configuration changes and bypass flips apply a
 * short fade-in to avoid clicks. Offline renderers should disable this so
 * renders are exact from sample zero. */
void eqf_chain_set_fade(eqf_chain *c, bool enabled);

/* Process planar float buffers. in and out may alias. `frames` must be <=
 * max_block given at construction. Returns EQF_OK or an error. */
int eqf_chain_process(eqf_chain *c, float *const *in, float *const *out, uint32_t frames);

/* Latency in frames introduced by the current configuration. */
uint32_t eqf_chain_latency(const eqf_chain *c);

/* Most recent meter/state snapshot. */
const eqf_chain_stats *eqf_chain_snapshot(const eqf_chain *c);
const eqf_chain_stats *eqf_chain_meters(const eqf_chain *c);

/* Last error message (per chain). */
const char *eqf_chain_last_error(const eqf_chain *c);

/* Direct access to sub-blocks (advanced embedding / tests). */
const eqf_eq *eqf_chain_eq(const eqf_chain *c);

/* Serialized current dsp config (JSON). Caller must not free. Valid until
 * next configure call. */
const char *eqf_chain_current_config(const eqf_chain *c);

#ifdef __cplusplus
}
#endif

#endif /* EQFORGE_CHAIN_H */
