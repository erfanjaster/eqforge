/* EQForge native core - shared types.
 *
 * SPDX-License-Identifier: MIT
 */
#ifndef EQFORGE_TYPES_H
#define EQFORGE_TYPES_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Error codes returned by eqf_* functions. */
enum eqf_result {
    EQF_OK = 0,
    EQF_ERR_INVALID_ARG = -1,
    EQF_ERR_NO_MEMORY = -2,
    EQF_ERR_IO = -3,
    EQF_ERR_PARSE = -4,
    EQF_ERR_UNSUPPORTED = -5,
    EQF_ERR_STATE = -6,
};

/* Biquad filter shapes (RBJ audio EQ cookbook). */
enum eqf_filter_type {
    EQF_FILTER_PEAK = 0,      /* peaking / bell */
    EQF_FILTER_LOW_SHELF,
    EQF_FILTER_HIGH_SHELF,
    EQF_FILTER_LOWPASS,
    EQF_FILTER_HIGHPASS,
    EQF_FILTER_BANDPASS,      /* constant 0 dB peak gain */
    EQF_FILTER_NOTCH,
    EQF_FILTER_ALLPASS,
    EQF_FILTER_LOWSHELF_12DB, /* 12 dB/octave shelves (Q-based) */
    EQF_FILTER_HIGHSHELF_12DB,
    EQF_FILTER__COUNT
};

/* Limits that keep the engine predictable and RT-safe. */
#define EQF_MAX_CHANNELS   16
#define EQF_MAX_BANDS      64   /* per channel */
#define EQF_MAX_FIR_TAPS   8192
#define EQF_MAX_BLOCK      8192

static inline const char *eqf_result_str(int r)
{
    switch (r) {
    case EQF_OK:               return "ok";
    case EQF_ERR_INVALID_ARG:  return "invalid argument";
    case EQF_ERR_NO_MEMORY:    return "out of memory";
    case EQF_ERR_IO:           return "i/o error";
    case EQF_ERR_PARSE:        return "parse error";
    case EQF_ERR_UNSUPPORTED:  return "unsupported";
    case EQF_ERR_STATE:        return "invalid state";
    default:                   return "unknown error";
    }
}

#ifdef __cplusplus
}
#endif

#endif /* EQFORGE_TYPES_H */
