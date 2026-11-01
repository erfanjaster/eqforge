/* EQForge native core - thin atomics shim.
 *
 * The core is C11, but `make -C core test-cxx` also compiles every source as
 * C++ to catch accidental C-only constructs. `_Atomic` and <stdatomic.h> do
 * not exist in C++, so the handle used across the code base is abstracted
 * here: in C it is a real C11 atomic, in C++ it is a std::atomic of the same
 * width with equivalent semantics.
 *
 * Only the operations the core actually needs are provided.
 *
 * SPDX-License-Identifier: MIT
 */
#ifndef EQFORGE_ATOMICS_H
#define EQFORGE_ATOMICS_H

#ifdef __cplusplus

#include <atomic>

#define EQF_ATOMIC(T) std::atomic<T>
#define EQF_MO_RELAXED std::memory_order_relaxed
#define EQF_MO_ACQUIRE std::memory_order_acquire
#define EQF_MO_RELEASE std::memory_order_release
#define EQF_ATOMIC_LOAD(p, mo) (p)->load(mo)
#define EQF_ATOMIC_STORE(p, v, mo) (p)->store((v), (mo))
#define EQF_ATOMIC_FENCE() std::atomic_thread_fence(std::memory_order_seq_cst)

#else

#include <stdatomic.h>

#define EQF_ATOMIC(T) _Atomic(T)
#define EQF_MO_RELAXED memory_order_relaxed
#define EQF_MO_ACQUIRE memory_order_acquire
#define EQF_MO_RELEASE memory_order_release
#define EQF_ATOMIC_LOAD(p, mo) atomic_load_explicit((p), (mo))
#define EQF_ATOMIC_STORE(p, v, mo) atomic_store_explicit((p), (v), (mo))
#define EQF_ATOMIC_FENCE() atomic_thread_fence(memory_order_seq_cst)

#endif

#endif /* EQFORGE_ATOMICS_H */
