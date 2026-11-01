/* EQForge native core - public facade.
 *
 * libeqforge is a dependency-free, real-time-safe C library implementing the
 * EQForge DSP engine: parametric EQ, dynamics, crossfeed, FIR convolution,
 * metering and a processing chain configured from JSON.
 *
 * Design goals:
 *  - No allocation, locking or syscalls inside process() paths (RT-safe).
 *  - Deterministic float math; identical results across compilers where
 *    IEEE-754 semantics hold (validated by the cross-implementation tests
 *    against the Python/numpy reference).
 *  - C11, written in a C++-compatible subset for a future native C++ port.
 *
 * SPDX-License-Identifier: MIT
 */
#ifndef EQFORGE_H
#define EQFORGE_H

#include "version.h"
#include "types.h"
#include "json.h"
#include "biquad.h"
#include "eq.h"
#include "dynamics.h"
#include "crossfeed.h"
#include "fir.h"
#include "meter.h"
#include "chain.h"

#ifdef __cplusplus
extern "C" {
#endif

const char *eqf_version_string(void);
uint32_t eqf_abi_version(void);

/* Configure a chain from a resolved dsp-config JSON file, convenience
 * wrapper used by embedded hosts (PipeWire module, RT host). */
eqf_chain *eqf_chain_open_file(uint32_t channels, uint32_t sample_rate,
                               uint32_t max_block, const char *config_path);

#ifdef __cplusplus
}
#endif

#endif /* EQFORGE_H */
