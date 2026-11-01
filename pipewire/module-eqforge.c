/* EQForge PipeWire filter node (SPA handle factory "eqforge.filter").
 *
 * A system-wide audio filter backed by libeqforge. Processes F32 (planar or
 * interleaved) through the EQForge chain configured from a resolved
 * dsp-config JSON file, and transparently reloads when that file changes
 * (mtime checked ~1x/sec; reconfigure is click-guarded by the core).
 *
 * Host it with libpipewire-module-adapter (see packaging/pipewire/):
 *
 *   pw-cli create-node adapter '{ factory.name = "eqforge.filter",
 *       node.name = "eqforge_filter", node.description = "EQForge",
 *       media.class = "Audio/Sink",
 *       config = "/home/user/.config/eqforge/active-dsp.json" }'
 *
 * or set EQFORGE_CONFIG in the PipeWire environment.
 *
 * RT-safety: process() never allocates; reload performs stat() + chain
 * reconfigure at ~1 Hz on the data thread (documented trade-off; a future
 * revision can move reload to a main-thread timer via SPA loop).
 *
 * SPDX-License-Identifier: MIT
 */

#ifndef _POSIX_C_SOURCE
#define _POSIX_C_SOURCE 200809L
#endif

#include <assert.h>
#include <errno.h>
#include <stdarg.h>
#include <limits.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#include <spa/buffer/buffer.h>
#include <spa/buffer/meta.h>
#include <spa/param/audio/format-utils.h>
#include <spa/param/format-utils.h>
#include <spa/param/latency-utils.h>
#include <spa/param/props.h>
#include <spa/param/param.h>
#include <spa/param/props.h>
#include <spa/support/log.h>
#include <spa/support/plugin.h>
#include <spa/node/io.h>
#include <spa/node/node.h>
#include <spa/node/utils.h>
#include <spa/utils/defs.h>
#include <spa/utils/result.h>

#include "eqforge/eqforge.h"

#ifndef PATH_MAX
#define PATH_MAX 4096
#endif

#define NAME "eqforge"
#define FACTORY_NAME "eqforge.filter"
#define DEFAULT_CONFIG_REL ".config/eqforge/active-dsp.json"
#define MAX_FRAMES 8192

/* Effect-specific SPA prop ids start after SPA_PROP_START_CUSTOM. */
#define EQF_PROP_START (SPA_PROP_START_CUSTOM)
#define EQF_PROP_bypass (EQF_PROP_START + 0)
#define MAX_BUFFERS 64

struct port {
    struct spa_io_buffers *io;
    struct spa_buffer *buffers[MAX_BUFFERS];
    uint32_t n_buffers;
};

struct impl {
    struct spa_node node;
    struct spa_hook_list hooks;
    const struct spa_node_callbacks *callbacks;
    struct spa_log *log;

    struct port ports[2]; /* [0] input, [1] output */

    struct spa_audio_info_raw format;
    bool has_format;

    /* spa_pod members include int64/uint64, so the builder requires 8-byte
     * alignment. A bare uint8_t array only guarantees 1-byte alignment, which
     * the SPA builder trips over (UB, and a real SIGBUS risk on strict-alignment
     * targets). */
    _Alignas(8) uint8_t pod_buffer[8192];

    /* Two chains are kept alive so a config change can be prepared off the
     * audio thread and swapped in atomically (see impl_process). */
    /* Ownership rules for chains, which exist because the data thread must
     * never allocate or free:
     *   - `chain` is published atomically. Only the control thread ever
     *     writes it (during configure_format(), between data periods).
     *   - `spare` is touched only by the control thread / watcher.
     *   - `retired` lists chains the data thread has finished with; the
     *     control thread frees them, so the RT thread never calls free().
     *     Bounded: if it were ever full we refuse to stage rather than leak. */
    _Atomic(eqf_chain *) chain;   /* what the data thread is processing */
    _Atomic(eqf_chain *) chain_next; /* published, awaiting swap */
    eqf_chain *spare;             /* being (re)configured off-thread */
    /* Single-producer (data thread) / single-consumer (control) hand-off of
     * chains awaiting free(). Monotonic counters, so a collector pass that
     * races with a new retirement cannot lose an entry (a plain "reset
     * counter to 0" scheme would drop it and leak the chain). */
    eqf_chain *retired[8];
    _Atomic uint32_t retired_head;   /* written by the data thread */
    _Atomic uint32_t retired_tail;   /* written by the control thread */
    char config_path[PATH_MAX];
    /* full-resolution (nanosecond) config stamp. Comparing only st_mtime gave
     * one-second granularity, so a config rewritten in the same second as the
     * previous load - exactly what happens when the daemon publishes a new
     * active-dsp.json right after start-up - was silently ignored. */
    struct timespec config_stamp;
    long reload_counter;       /* audio-thread tick counter (no stat needed) */
    long stat_counter;         /* stat() calls are only made every Nth tick */
    _Atomic int swap_requested; /* set by the control thread when ready */
    _Atomic int bypass_requested;
    _Atomic bool bypass;

    /* planar deinterleave scratch: no stack/VLA allocation in process() */
    float tmp_in[EQF_MAX_CHANNELS][MAX_FRAMES];
    float tmp_out[EQF_MAX_CHANNELS][MAX_FRAMES];

    uint32_t channels;
    uint32_t rate;
    /* atomic mirrors read by the config watcher thread (which must not race
     * with format negotiation on the main thread) */
    _Atomic uint32_t cfg_channels;
    _Atomic uint32_t cfg_rate;

    /* ~2 Hz config watcher: does the stat() + parse + stage work off the data
     * thread so process() never performs I/O or allocation. */
    pthread_t watcher;
    _Atomic bool watcher_run;
    bool watcher_started;
};

static bool impl_update_config(struct impl *this, bool force);
static inline void impl_apply_pending_swap(struct impl *this);
static void impl_collect_retired(struct impl *this);

/* Prepare a reconfigured chain in this->spare and ask the data thread to swap
 * it in. Called from the control thread (or from process() as a last resort
 * when the host gave us no other context) - never mutates `this->chain`, so
 * the audio thread is never handed a half-built graph. */
/* Hosts frequently pass no SPA log interface (the standalone smoke test, for
 * instance). Without a fallback every diagnostic was silently discarded, which
 * made a mis-configured node indistinguishable from a silent one. */
static void eqf_log(struct impl *this, int level, const char *fmt, ...)
{
    va_list ap;
    if (this->log) {
        char buf[512];
        va_start(ap, fmt);
        vsnprintf(buf, sizeof(buf), fmt, ap);
        va_end(ap);
        if (level <= 1)
            spa_log_error(this->log, NAME ": %s", buf);
        else if (level == 2)
            spa_log_warn(this->log, NAME ": %s", buf);
        else
            spa_log_info(this->log, NAME ": %s", buf);
        return;
    }
    if (getenv("EQFORGE_DEBUG")) {
        va_start(ap, fmt);
        fprintf(stderr, "eqforge: ");
        vfprintf(stderr, fmt, ap);
        fprintf(stderr, "\n");
        va_end(ap);
    }
}

static bool impl_update_config(struct impl *this, bool force)
{
    /* Note: this must NOT require this->chain. The very first configuration is
     * staged before any live chain exists (configure_format() only allocates
     * the spare), so an early return here left the node passing audio through
     * unprocessed for its whole lifetime. */
    if (this->config_path[0] == '\0')
        return false;

    struct stat st;
    if (stat(this->config_path, &st) != 0)
        return false;
    if (!force && st.st_mtim.tv_sec == this->config_stamp.tv_sec &&
        st.st_mtim.tv_nsec == this->config_stamp.tv_nsec)
        return false;
    this->config_stamp = st.st_mtim;

    if (!this->spare) {
        uint32_t ch = atomic_load(&this->cfg_channels);
        uint32_t rate = atomic_load(&this->cfg_rate);
        if (ch == 0)
            ch = 2;
        if (rate == 0)
            rate = 48000;
        this->spare = eqf_chain_new(ch, rate, MAX_FRAMES);
        if (!this->spare) {
            eqf_log(this, 1, "cannot allocate standby chain");
            return false;
        }
    }
    int rc = eqf_chain_configure_file(this->spare, this->config_path);
    if (rc != EQF_OK) {
        eqf_log(this, 1, "config '%s' rejected (%s): %s", this->config_path,
                eqf_result_str(rc),
                eqf_chain_last_error(this->spare) ?: "no detail");
        return false;
    }
    /* Publish the freshly built chain for the data thread, then release-store
     * the request flag. The chain is not referenced anywhere else afterwards. */
    atomic_store_explicit(&this->chain_next, this->spare, memory_order_release);
    this->spare = NULL;
    atomic_store_explicit(&this->swap_requested, 1, memory_order_release);
    eqf_log(this, 3, "config '%s' staged for swap", this->config_path);
    return true;
}

/* Config watcher: the only thread that touches the filesystem after start-up.
 * Polls the config file's mtime at ~2 Hz (stat is cheap, and it happens here,
 * not in the data callback) and stages a rebuilt chain when it changes. */
static void *config_watcher(void *arg)
{
    struct impl *this = (struct impl *)arg;
    while (atomic_load(&this->watcher_run)) {
        struct timespec ts = { .tv_sec = 0, .tv_nsec = 500000000 };
        nanosleep(&ts, NULL);
        impl_collect_retired(this);
        if (atomic_load(&this->swap_requested))
            continue;          /* previous reload not consumed yet */
        impl_update_config(this, false);
    }
    return NULL;
}

static void watcher_start(struct impl *this)
{
    if (this->watcher_started || this->config_path[0] == '\0')
        return;
    atomic_store(&this->watcher_run, true);
    if (pthread_create(&this->watcher, NULL, config_watcher, this) == 0) {
        this->watcher_started = true;
    } else {
        atomic_store(&this->watcher_run, false);
        eqf_log(this, 2, "cannot start config watcher; config changes need a "
                 "node restart");
    }
}

static void watcher_stop(struct impl *this)
{
    if (!this->watcher_started)
        return;
    atomic_store(&this->watcher_run, false);
    pthread_join(this->watcher, NULL);
    this->watcher_started = false;
}

/* Called on the audio thread: consume a staged configuration. This is a
 * pointer swap plus one free() of the retired chain - the worst case is a
 * bounded deallocation, no parsing, no I/O, no allocation. */
static inline void impl_apply_pending_swap(struct impl *this)
{
    /* Called on the audio thread: no allocation, no free, no I/O. The new
     * pointer was published by the control thread before swap_requested was
     * raised (release store), so this acquire load observes a fully built
     * chain. */
    if (!atomic_load_explicit(&this->swap_requested, memory_order_acquire))
        return;
    eqf_chain *next = atomic_load_explicit(&this->chain_next,
                                           memory_order_acquire);
    if (!next)
        return;
    eqf_chain *old = atomic_exchange(&this->chain, next);
    atomic_store_explicit(&this->chain_next, NULL, memory_order_release);
    atomic_store_explicit(&this->swap_requested, 0, memory_order_release);
    if (old) {
        uint32_t head = atomic_load_explicit(&this->retired_head,
                                             memory_order_relaxed);
        uint32_t tail = atomic_load_explicit(&this->retired_tail,
                                             memory_order_acquire);
        if (head - tail >= SPA_N_ELEMENTS(this->retired)) {
            /* Collector has not run for 8 swaps: the control thread is wedged.
             * Freeing here is an RT violation but strictly better than leaking
             * a chain every reload. */
            eqf_chain_free(old);
        } else {
            this->retired[head % SPA_N_ELEMENTS(this->retired)] = old;
            atomic_store_explicit(&this->retired_head, head + 1,
                                  memory_order_release);
        }
    }
}

/* Control-thread side: free chains the data thread has released. Bounded and
 * allocation-free; safe to call from the watcher every poll. */
static void impl_collect_retired(struct impl *this)
{
    uint32_t head = atomic_load_explicit(&this->retired_head,
                                         memory_order_acquire);
    uint32_t tail = atomic_load_explicit(&this->retired_tail,
                                         memory_order_relaxed);
    while (tail != head) {
        eqf_chain_free(this->retired[tail % SPA_N_ELEMENTS(this->retired)]);
        this->retired[tail % SPA_N_ELEMENTS(this->retired)] = NULL;
        tail++;
    }
    /* Only advance after the frees, so a producer that observes the new tail
     * can never reuse a slot that is still being freed. */
    atomic_store_explicit(&this->retired_tail, tail, memory_order_release);
}

/* ------------------------------------------------------------------ */

/* Announce the node and its (static, single-per-direction multichannel)
 * ports to a freshly added listener. PipeWire's module-adapter counts
 * port_info events in pw_adapter_new() and refuses nodes that announce no
 * ports (-EINVAL), so emitting these on add_listener is mandatory for the
 * node to be hostable by a real PipeWire daemon. Pattern mirrors
 * spa/plugins/support/null-audio-sink.c. */
static void emit_node_info_ev(struct impl *this)
{
    struct spa_node_info info;
    static const struct spa_dict_item items[] = {
        { SPA_KEY_FACTORY_NAME, FACTORY_NAME },
    };
    memset(&info, 0, sizeof(info));
    info.change_mask = SPA_NODE_CHANGE_MASK_FLAGS |
                       SPA_NODE_CHANGE_MASK_PROPS;
    info.flags = SPA_NODE_FLAG_RT;
    info.max_input_ports = 1;
    info.max_output_ports = 1;
    info.props = &SPA_DICT_INIT_ARRAY(items);
    spa_node_emit_info(&this->hooks, &info);
}

static void emit_port_info_ev(struct impl *this, enum spa_direction direction,
                              uint32_t port_id)
{
    struct spa_port_info pinfo;
    memset(&pinfo, 0, sizeof(pinfo));
    pinfo.change_mask = SPA_PORT_CHANGE_MASK_FLAGS;
    pinfo.flags = SPA_PORT_FLAG_LIVE;
    spa_node_emit_port_info(&this->hooks, direction, port_id, &pinfo);
}

static int impl_add_listener(void *object, struct spa_hook *listener,
                             const struct spa_node_events *events, void *data)
{
    struct impl *this = object;
    struct spa_hook_list save;

    /* isolate: emit the info events to the NEW listener only */
    spa_hook_list_isolate(&this->hooks, &save, listener, events, data);

    emit_node_info_ev(this);
    emit_port_info_ev(this, SPA_DIRECTION_INPUT, 0);
    emit_port_info_ev(this, SPA_DIRECTION_OUTPUT, 0);

    spa_hook_list_join(&this->hooks, &save);
    return 0;
}

static int impl_set_callbacks(void *object, const struct spa_node_callbacks *callbacks,
                              void *data)
{
    struct impl *this = object;
    (void)data;
    this->callbacks = callbacks;
    return 0;
}

static int impl_sync(void *object, int seq)
{
    /* fully synchronous node */
    (void)object; (void)seq;
    return 0;
}

static int impl_enum_params(void *object, int seq, uint32_t id,
                            uint32_t start, uint32_t num,
                            const struct spa_pod *filter)
{
    struct impl *this = object;
    (void)filter;
    struct spa_pod_builder b = SPA_POD_BUILDER_INIT(this->pod_buffer,
                                                    sizeof(this->pod_buffer));
    struct spa_pod *params[3];
    uint32_t n_params = 0;

    if (num == 0)
        return 0;

    switch (id) {
    case SPA_PARAM_EnumFormat:
        /* F32P first: PipeWire's graph runs planar F32 natively, and
         * audioadapt can then use passthrough mode (no internal convert
         * node). Interleaved F32 remains as a fallback for hosts that
         * negotiate it. */
        params[n_params++] = spa_pod_builder_add_object(&b,
            SPA_TYPE_OBJECT_Format, SPA_PARAM_EnumFormat,
            SPA_FORMAT_mediaType, SPA_POD_Id(SPA_MEDIA_TYPE_audio),
            SPA_FORMAT_mediaSubtype, SPA_POD_Id(SPA_MEDIA_SUBTYPE_raw),
            SPA_FORMAT_AUDIO_format, SPA_POD_Id(SPA_AUDIO_FORMAT_F32P),
            SPA_FORMAT_AUDIO_channels, SPA_POD_CHOICE_RANGE_Int(2, 1, 8),
            SPA_FORMAT_AUDIO_rate, SPA_POD_CHOICE_RANGE_Int(48000, 8000, 384000));
        params[n_params++] = spa_pod_builder_add_object(&b,
            SPA_TYPE_OBJECT_Format, SPA_PARAM_EnumFormat,
            SPA_FORMAT_mediaType, SPA_POD_Id(SPA_MEDIA_TYPE_audio),
            SPA_FORMAT_mediaSubtype, SPA_POD_Id(SPA_MEDIA_SUBTYPE_raw),
            SPA_FORMAT_AUDIO_format, SPA_POD_Id(SPA_AUDIO_FORMAT_F32),
            SPA_FORMAT_AUDIO_channels, SPA_POD_CHOICE_RANGE_Int(2, 1, 8),
            SPA_FORMAT_AUDIO_rate, SPA_POD_CHOICE_RANGE_Int(48000, 8000, 384000));
        break;

    case SPA_PARAM_Format:
        if (!this->has_format)
            return 0;
        {
            struct spa_audio_info info;
            memset(&info, 0, sizeof(info));
            info.media_type = SPA_MEDIA_TYPE_audio;
            info.media_subtype = SPA_MEDIA_SUBTYPE_raw;
            info.info.raw = this->format;
            params[n_params++] = spa_format_audio_build(&b, SPA_PARAM_Format,
                                                        &info);
        }
        break;

    case SPA_PARAM_IO:
        params[n_params++] = spa_pod_builder_add_object(&b,
            SPA_TYPE_OBJECT_ParamIO, SPA_PARAM_IO,
            SPA_PARAM_IO_id, SPA_POD_Id(SPA_IO_Buffers),
            SPA_PARAM_IO_size, SPA_POD_Int(sizeof(struct spa_io_buffers)));
        break;

    case SPA_PARAM_Latency:
    {
        uint64_t ns = 0;
        eqf_chain *live = atomic_load(&this->chain);
        if (live && this->rate > 0) {
            uint32_t lat = eqf_chain_latency(live);
            ns = (uint64_t)lat * SPA_NSEC_PER_SEC / this->rate;
        }
        struct spa_latency_info latency =
            SPA_LATENCY_INFO(SPA_DIRECTION_OUTPUT, .min_ns = ns, .max_ns = ns);
        params[n_params++] = spa_latency_build(&b, SPA_PARAM_Latency, &latency);
        break;
    }
    default:
        return -ENOENT;
    }

    for (uint32_t i = 0; i < n_params; i++) {
        if (start > i)
            continue;
        struct spa_result_node_params rp = {
            .index = i, .next = i + 1, .param = params[i] };
        spa_node_emit_result(&this->hooks, seq, 0, SPA_RESULT_TYPE_NODE_PARAMS,
                             &rp);
        if (start + num <= i + 1)
            break;
    }
    return 1;
}

static int impl_configure_format(struct impl *this,
                                 const struct spa_audio_info_raw *info)
{
    this->format = *info;
    this->channels = info->channels;
    this->rate = info->rate;
    if (this->channels == 0 || this->channels > EQF_MAX_CHANNELS)
        this->channels = 2;
    if (this->rate == 0)
        this->rate = 48000;

    /* Format negotiation happens on the control/main thread, before or
     * between data-loop periods, so rebuilding here is fine - but only the
     * *spare* slot is rebuilt; the live chain is swapped in on the data
     * thread so the RT thread never sees a partially configured graph.
     * The previous live chain may still be in use by a data loop that has not
     * yet run its swap, so it is retired through the collector rather than
     * freed here.
     * NOTE: concurrent impl_process() calls remain unsupported (as they were
     * before this change) - a format renegotiation while a data loop is
     * actively running is undefined for the whole module. */
    impl_collect_retired(this);
    if (this->spare) {
        eqf_chain_free(this->spare);
        this->spare = NULL;
    }
    atomic_store(&this->swap_requested, 0);
    atomic_store(&this->chain_next, NULL);

    this->spare = eqf_chain_new(this->channels, this->rate, MAX_FRAMES);
    if (!this->spare) {
        eqf_log(this, 1, "failed to allocate chain (%u ch, %u Hz)",
                this->channels, this->rate);
        return -ENOMEM;
    }
    atomic_store(&this->cfg_channels, this->channels);
    atomic_store(&this->cfg_rate, this->rate);
    memset(&this->config_stamp, 0, sizeof(this->config_stamp));
    this->config_stamp.tv_nsec = -1;  /* force a (re)load for the new format */
    bool staged = impl_update_config(this, true);   /* may stage over `spare` */
    if (!staged) {
        /* No usable config file: publish the empty chain so the node still
         * processes (pass-through) rather than running with a stale graph of
         * the wrong channel count or sample rate. */
        eqf_log(this, 2, "no usable config for %u ch @ %u Hz; "
                "running pass-through", this->channels, this->rate);
        atomic_store_explicit(&this->chain_next, this->spare,
                              memory_order_release);
        this->spare = NULL;
        atomic_store_explicit(&this->swap_requested, 1, memory_order_release);
    }
    if (atomic_load(&this->swap_requested)) {
        /* swap immediately: no data has flowed yet for this format */
        impl_apply_pending_swap(this);
    }
    this->has_format = true;
    return 0;
}

static int parse_raw_format(const struct spa_pod *param,
                            struct spa_audio_info_raw *raw)
{
    struct spa_audio_info info;
    memset(&info, 0, sizeof(info));
    memset(raw, 0, sizeof(*raw));
    if (spa_format_parse(param, &info.media_type, &info.media_subtype) < 0)
        return -EINVAL;
    if (info.media_type != SPA_MEDIA_TYPE_audio ||
        info.media_subtype != SPA_MEDIA_SUBTYPE_raw)
        return -EINVAL;
    if (spa_format_audio_raw_parse(param, raw) < 0)
        return -EINVAL;
    return 0;
}

/* Apply SPA props. Supported today:
 *   SPA_PROP_mute       - bypass processing entirely (true = pass through)
 *   SPA_PROP_softMute   - same, for hosts that use the software mute flag
 * Anything else in the pod is ignored (hosts routinely send a whole props
 * object), but the pod must at least be a props object.
 *
 * Before this existed, SPA_PARAM_Props was accepted and thrown away, so a host
 * could mute the node and hear no change - and the bypass field the audio
 * thread reads was never written by anything at all. */
static int impl_set_props(struct impl *this, const struct spa_pod *param)
{
    if (!spa_pod_is_object_type(param, SPA_TYPE_OBJECT_Props))
        return -EINVAL;

    const struct spa_pod_object *obj = (const struct spa_pod_object *)param;
    const struct spa_pod_prop *p;
    int rc = 0;

    p = spa_pod_object_find_prop(obj, NULL, SPA_PROP_mute);
    if (p) {
        bool v = false;
        if (spa_pod_get_bool(&p->value, &v) == 0)
            atomic_store(&this->bypass_requested, v ? 1 : 0);
        else
            rc = -EINVAL;
    }
    p = spa_pod_object_find_prop(obj, NULL, SPA_PROP_softMute);
    if (p) {
        bool v = false;
        if (spa_pod_get_bool(&p->value, &v) != 0)
            rc = -EINVAL;
        else if (!spa_pod_object_find_prop(obj, NULL, SPA_PROP_mute))
            atomic_store(&this->bypass_requested, v ? 1 : 0);
    }
    /* An explicit effect-specific key takes precedence when present. */
    p = spa_pod_object_find_prop(obj, NULL, EQF_PROP_bypass);
    if (p) {
        bool v = false;
        if (spa_pod_get_bool(&p->value, &v) == 0)
            atomic_store(&this->bypass_requested, v ? 1 : 0);
        else
            rc = -EINVAL;
    }
    return rc;
}

static int impl_set_param(void *object, uint32_t id, uint32_t flags,
                          const struct spa_pod *param)
{
    struct impl *this = object;
    (void)flags;

    if (id == 0 || param == NULL)
        return -EINVAL;

    switch (id) {
    case SPA_PARAM_Format:
    {
        struct spa_audio_info_raw raw;
        if (parse_raw_format(param, &raw) < 0)
            return -EINVAL;
        if (raw.format != SPA_AUDIO_FORMAT_F32 &&
            raw.format != SPA_AUDIO_FORMAT_F32P)
            return -EINVAL;
        return impl_configure_format(this, &raw);
    }
    case SPA_PARAM_Latency:
        /* Read-only in this node: the latency follows the limiter look-ahead
         * and is reported through enum_params. Accepting a set here without
         * doing anything would be a silent lie. */
        return -EINVAL;

    case SPA_PARAM_Props:
        return impl_set_props(this, param);

    default:
        return -ENOENT;
    }
}

static int impl_set_io(void *object, uint32_t id, void *data, size_t size)
{
    (void)object; (void)data; (void)size;
    /* PipeWire/audioadapt hand us Clock/Position/RateMatch; the chain is
     * driven by the graph and does not need them, but refusing these ids
     * makes activation fail in some hosts, so accept and ignore. */
    switch (id) {
    case SPA_IO_Clock:
    case SPA_IO_Position:
    case SPA_IO_RateMatch:
    case SPA_IO_Buffers:
        return 0;
    default:
        return -ENOENT;
    }
}

static int impl_send_command(void *object, const struct spa_command *command)
{
    (void)object;
    switch (SPA_NODE_COMMAND_ID(command)) {
    case SPA_NODE_COMMAND_Start:
        /* format + buffers are in place by the time the graph starts us;
         * process() tolerates missing input and stays click-free. */
        return 0;
    case SPA_NODE_COMMAND_Suspend:
    case SPA_NODE_COMMAND_Pause:
        return 0;
    default:
        return -ENOTSUP;
    }
}

static int impl_add_port(void *object, enum spa_direction direction,
                         uint32_t port_id, const struct spa_dict *props)
{
    struct impl *this = object;
    (void)props;
    if (port_id != 0)
        return -EINVAL;
    if (direction != SPA_DIRECTION_INPUT && direction != SPA_DIRECTION_OUTPUT)
        return -EINVAL;
    int idx = direction == SPA_DIRECTION_OUTPUT ? 1 : 0;
    this->ports[idx].io = NULL;
    this->ports[idx].n_buffers = 0;

    struct spa_port_info pinfo;
    memset(&pinfo, 0, sizeof(pinfo));
    pinfo.flags = SPA_PORT_FLAG_LIVE;
    pinfo.change_mask = SPA_PORT_CHANGE_MASK_FLAGS;
    spa_node_emit_port_info(&this->hooks, direction, port_id, &pinfo);
    return 0;
}

static int impl_remove_port(void *object, enum spa_direction direction,
                            uint32_t port_id)
{
    (void)object; (void)direction; (void)port_id;
    return 0;
}

static int impl_port_enum_params(void *object, int seq,
                                 enum spa_direction direction, uint32_t port_id,
                                 uint32_t id, uint32_t start, uint32_t num,
                                 const struct spa_pod *filter)
{
    struct impl *this = object;
    (void)direction; (void)port_id;
    struct spa_pod_builder b = SPA_POD_BUILDER_INIT(this->pod_buffer,
                                                    sizeof(this->pod_buffer));
    struct spa_pod *params[3];
    uint32_t n_params = 0;

    if (num == 0)
        return 0;

    switch (id) {
    case SPA_PARAM_EnumFormat:
        /* PipeWire's module-adapter probes the *port-level* EnumFormat via
         * spa_node_port_enum_params_sync() (find_format) to detect the media
         * type; without it the adapter refuses to host the node. The port
         * format equals the node format (single multichannel port).
         * F32P first so audioadapt can pick passthrough (see node-level
         * EnumFormat); the shared pod buffer holds both objects. */
        params[n_params++] = spa_pod_builder_add_object(&b,
            SPA_TYPE_OBJECT_Format, SPA_PARAM_EnumFormat,
            SPA_FORMAT_mediaType, SPA_POD_Id(SPA_MEDIA_TYPE_audio),
            SPA_FORMAT_mediaSubtype, SPA_POD_Id(SPA_MEDIA_SUBTYPE_raw),
            SPA_FORMAT_AUDIO_format, SPA_POD_Id(SPA_AUDIO_FORMAT_F32P),
            SPA_FORMAT_AUDIO_channels, SPA_POD_CHOICE_RANGE_Int(2, 1, 8),
            SPA_FORMAT_AUDIO_rate, SPA_POD_CHOICE_RANGE_Int(48000, 8000, 384000));
        params[n_params++] = spa_pod_builder_add_object(&b,
            SPA_TYPE_OBJECT_Format, SPA_PARAM_EnumFormat,
            SPA_FORMAT_mediaType, SPA_POD_Id(SPA_MEDIA_TYPE_audio),
            SPA_FORMAT_mediaSubtype, SPA_POD_Id(SPA_MEDIA_SUBTYPE_raw),
            SPA_FORMAT_AUDIO_format, SPA_POD_Id(SPA_AUDIO_FORMAT_F32),
            SPA_FORMAT_AUDIO_channels, SPA_POD_CHOICE_RANGE_Int(2, 1, 8),
            SPA_FORMAT_AUDIO_rate, SPA_POD_CHOICE_RANGE_Int(48000, 8000, 384000));
        break;

    case SPA_PARAM_Format:
        if (this->has_format) {
            struct spa_audio_info info;
            memset(&info, 0, sizeof(info));
            info.media_type = SPA_MEDIA_TYPE_audio;
            info.media_subtype = SPA_MEDIA_SUBTYPE_raw;
            info.info.raw = this->format;
            params[n_params++] = spa_format_audio_build(&b, SPA_PARAM_Format,
                                                        &info);
        }
        break;

    case SPA_PARAM_Buffers:
    {
        /* blocks must match the negotiated layout: 1 for interleaved F32,
         * one per channel for planar F32P. Declaring per-channel blocks for
         * interleaved audio makes hosts allocate extra datas that the
         * planar-detection heuristic in process_buffers would misread. */
        uint32_t ch = this->has_format ? this->format.channels : 2;
        bool planar = this->has_format &&
                      this->format.format == SPA_AUDIO_FORMAT_F32P;
        uint32_t blocks = planar ? ch : 1;
        /* Fully fixated values (no Choice pods): audioadapt feeds this pod
         * back as a *filter* into the convert node's Buffers enum, and
         * spa_pod_filter() cannot intersect Choice properties on the filter
         * side - the negotiation then fails with -ENOTSUP. */
        params[n_params++] = spa_pod_builder_add_object(&b,
            SPA_TYPE_OBJECT_ParamBuffers, SPA_PARAM_Buffers,
            SPA_PARAM_BUFFERS_buffers, SPA_POD_Int(4),
            SPA_PARAM_BUFFERS_blocks, SPA_POD_Int(blocks),
            SPA_PARAM_BUFFERS_size,
            SPA_POD_Int(sizeof(float) * MAX_FRAMES * (planar ? 1 : ch)),
            SPA_PARAM_BUFFERS_stride, SPA_POD_Int(sizeof(float)));
        break;
    }
    case SPA_PARAM_IO:
        params[n_params++] = spa_pod_builder_add_object(&b,
            SPA_TYPE_OBJECT_ParamIO, SPA_PARAM_IO,
            SPA_PARAM_IO_id, SPA_POD_Id(SPA_IO_Buffers),
            SPA_PARAM_IO_size, SPA_POD_Int(sizeof(struct spa_io_buffers)));
        break;
    case SPA_PARAM_Meta:
        params[n_params++] = spa_pod_builder_add_object(&b,
            SPA_TYPE_OBJECT_ParamMeta, SPA_PARAM_Meta,
            SPA_PARAM_META_type, SPA_POD_Id(SPA_META_Header),
            SPA_PARAM_META_size, SPA_POD_Int(sizeof(struct spa_meta_header)));
        break;
    case SPA_PARAM_Latency:
    case SPA_PARAM_ProcessLatency:
    {
        uint64_t ns = 0;
        eqf_chain *live = atomic_load(&this->chain);
        if (live && this->rate > 0) {
            uint32_t lat = eqf_chain_latency(live);
            ns = (uint64_t)lat * SPA_NSEC_PER_SEC / this->rate;
        }
        struct spa_latency_info latency =
            SPA_LATENCY_INFO(direction, .min_ns = ns, .max_ns = ns);
        params[n_params++] = spa_latency_build(&b, id, &latency);
        break;
    }
    default:
        return -ENOENT;
    }

    for (uint32_t i = 0; i < n_params; i++) {
        if (start > i)
            continue;
        struct spa_result_node_params rp = {
            .index = i, .next = i + 1, .param = params[i] };
        spa_node_emit_result(&this->hooks, seq, 0, SPA_RESULT_TYPE_NODE_PARAMS,
                             &rp);
        if (start + num <= i + 1)
            break;
    }
    return 1;
}

static int impl_port_set_param(void *object, enum spa_direction direction,
                               uint32_t port_id, uint32_t id, uint32_t flags,
                               const struct spa_pod *param)
{
    struct impl *this = object;
    (void)direction; (void)port_id; (void)flags;

    /* PipeWire configures the format through the *port* param path (see
     * audioadapter's configure_format); handling only the node-level
     * SPA_PARAM_Format left the node unconfigured and it failed to start. */
    if (id == SPA_PARAM_Format) {
        struct spa_audio_info_raw raw;
        if (param == NULL)
            return 0;
        if (parse_raw_format(param, &raw) < 0)
            return -EINVAL;
        if (raw.format != SPA_AUDIO_FORMAT_F32 &&
            raw.format != SPA_AUDIO_FORMAT_F32P)
            return -EINVAL;
        return impl_configure_format(this, &raw);
    }
    return 0;
}

static int impl_port_use_buffers(void *object, enum spa_direction direction,
                                 uint32_t port_id, uint32_t flags,
                                 struct spa_buffer **buffers, uint32_t n_buffers)
{
    struct impl *this = object;
    (void)port_id; (void)flags;
    int idx = direction == SPA_DIRECTION_OUTPUT ? 1 : 0;
    struct port *p = &this->ports[idx];
    if (n_buffers > MAX_BUFFERS)
        return -EINVAL;
    p->n_buffers = buffers ? n_buffers : 0;
    for (uint32_t i = 0; i < p->n_buffers; i++)
        p->buffers[i] = buffers[i];
    return 0;
}

static int impl_port_set_io(void *object, enum spa_direction direction,
                            uint32_t port_id, uint32_t id,
                            void *data, size_t size)
{
    struct impl *this = object;
    (void)port_id;
    int idx = direction == SPA_DIRECTION_OUTPUT ? 1 : 0;
    if (id == SPA_IO_Buffers) {
        if (data && size < sizeof(struct spa_io_buffers))
            return -EINVAL;
        this->ports[idx].io = data;
        return 0;
    }
    return -ENOENT;
}

static int impl_port_reuse_buffer(void *object, uint32_t port_id,
                                  uint32_t buffer_id)
{
    (void)object; (void)port_id; (void)buffer_id;
    return 0;
}

/* ------------------------------------------------------------------ */


static int process_buffers(struct impl *this, eqf_chain *chain,
                           struct spa_buffer *ib, struct spa_buffer *ob)
{
    uint32_t ch = this->channels;
    if (ch == 0 || ib->n_datas == 0 || ib->datas[0].chunk == NULL)
        return -EINVAL;

    /* planar when the buffer carries one spa_data per channel */
    bool planar = (ib->n_datas >= ch && ch > 1 && ib->datas[1].data != NULL &&
                   ib->datas[0].chunk->stride == (int32_t)sizeof(float));

    uint32_t frames;
    if (planar) {
        frames = ib->datas[0].chunk->size / sizeof(float);
    } else {
        frames = ib->datas[0].chunk->size / (sizeof(float) * ch);
    }
    if (frames > MAX_FRAMES)
        frames = MAX_FRAMES;
    if (frames == 0)
        return -EINVAL;

    if (planar && ob->n_datas >= ch) {
        float *ip[EQF_MAX_CHANNELS], *op[EQF_MAX_CHANNELS];
        for (uint32_t c = 0; c < ch; c++) {
            if (ib->datas[c].data == NULL || ob->datas[c].data == NULL ||
                ib->datas[c].chunk == NULL || ob->datas[c].chunk == NULL)
                return -EINVAL;
            if (ob->datas[c].maxsize < frames * sizeof(float))
                return -EINVAL;
            ip[c] = (float *)((uint8_t *)ib->datas[c].data +
                              ib->datas[c].chunk->offset);
            op[c] = (float *)((uint8_t *)ob->datas[c].data +
                              ob->datas[c].chunk->offset);
            ob->datas[c].chunk->offset = ib->datas[c].chunk->offset;
            ob->datas[c].chunk->size = frames * sizeof(float);
            ob->datas[c].chunk->stride = sizeof(float);
        }
        if (chain) {
            eqf_chain_process(chain, ip, op, frames);
        } else {
            for (uint32_t c = 0; c < ch; c++)
                memmove(op[c], ip[c], frames * sizeof(float));
        }
    } else {
        /* interleaved (or mono) */
        if (ob->datas[0].data == NULL || ob->datas[0].chunk == NULL ||
            ob->datas[0].maxsize < frames * ch * sizeof(float))
            return -EINVAL;
        const float *src = (const float *)((uint8_t *)ib->datas[0].data +
                                           ib->datas[0].chunk->offset);
        float *dst = (float *)((uint8_t *)ob->datas[0].data +
                               ob->datas[0].chunk->offset);
        ob->datas[0].chunk->offset = ib->datas[0].chunk->offset;
        ob->datas[0].chunk->size = frames * ch * sizeof(float);
        ob->datas[0].chunk->stride = sizeof(float);
        if (chain) {
            /* Deinterleave into the node's preallocated scratch. The previous
             * version put 1 MB of float arrays on the stack here, which is a
             * stack-overflow hazard in a host thread and defeats the "no
             * allocation in process()" guarantee. */
            float *ip[EQF_MAX_CHANNELS], *op[EQF_MAX_CHANNELS];
            for (uint32_t c = 0; c < ch; c++) {
                ip[c] = this->tmp_in[c];
                op[c] = this->tmp_out[c];
            }
            for (uint32_t i = 0; i < frames; i++)
                for (uint32_t c = 0; c < ch; c++)
                    ip[c][i] = src[i * ch + c];
            eqf_chain_process(chain, ip, op, frames);
            for (uint32_t i = 0; i < frames; i++)
                for (uint32_t c = 0; c < ch; c++)
                    dst[i * ch + c] = op[c][i];
        } else {
            memmove(dst, src, frames * ch * sizeof(float));
        }
    }
    return 0;
}

static int impl_process(void *object)
{
    struct impl *this = object;
    struct port *in = &this->ports[0];
    struct port *out = &this->ports[1];
    struct spa_io_buffers *ibi = in->io;
    struct spa_io_buffers *obi = out->io;

    if (obi == NULL)
        return SPA_STATUS_HAVE_DATA;

    /* Consume a configuration staged by the control thread (pointer swap). */
    impl_apply_pending_swap(this);
    eqf_chain *chain = atomic_load_explicit(&this->chain, memory_order_acquire);

    /* Propagate a props-driven mute onto the live chain. eqf_chain_set_bypass()
     * starts a crossfade, so it must only be called on an edge - calling it
     * every cycle would restart the fade forever and silence the node. */
    bool want_bypass = atomic_load(&this->bypass_requested) != 0;
    if (want_bypass != atomic_load(&this->bypass)) {
        atomic_store(&this->bypass, want_bypass);
        if (chain)
            eqf_chain_set_bypass(chain, want_bypass);
    }

    if (ibi == NULL || ibi->buffer_id >= in->n_buffers) {
        obi->buffer_id = SPA_ID_INVALID;
        return SPA_STATUS_HAVE_DATA;
    }
    struct spa_buffer *inb = in->buffers[ibi->buffer_id];
    if (inb == NULL) {
        obi->buffer_id = SPA_ID_INVALID;
        return SPA_STATUS_HAVE_DATA;
    }

    if (obi->buffer_id < out->n_buffers && out->buffers[obi->buffer_id]) {
        /* dedicated output buffer */
        if (process_buffers(this, chain, inb,
                            out->buffers[obi->buffer_id]) < 0)
            obi->buffer_id = SPA_ID_INVALID;
        return SPA_STATUS_HAVE_DATA;
    }

    /* host provided no output buffer id: process in place and forward the
     * input buffer */
    if (process_buffers(this, chain, inb, inb) < 0) {
        obi->buffer_id = SPA_ID_INVALID;
        return SPA_STATUS_HAVE_DATA;
    }
    obi->buffer_id = ibi->buffer_id;
    return SPA_STATUS_HAVE_DATA;
}

static const struct spa_node_methods impl_node_methods = {
    SPA_VERSION_NODE_METHODS,
    .add_listener = impl_add_listener,
    .set_callbacks = impl_set_callbacks,
    .sync = impl_sync,
    .enum_params = impl_enum_params,
    .set_param = impl_set_param,
    .set_io = impl_set_io,
    .send_command = impl_send_command,
    .add_port = impl_add_port,
    .remove_port = impl_remove_port,
    .port_enum_params = impl_port_enum_params,
    .port_set_param = impl_port_set_param,
    .port_use_buffers = impl_port_use_buffers,
    .port_set_io = impl_port_set_io,
    .port_reuse_buffer = impl_port_reuse_buffer,
    .process = impl_process,
};

/* ---------------- handle / factory ---------------- */

struct impl_handle {
    struct spa_handle handle;
    struct impl impl;
};

static int impl_handle_get_interface(struct spa_handle *handle, const char *type,
                                     void **interface)
{
    struct impl_handle *h = (struct impl_handle *)handle;
    if (strcmp(type, SPA_TYPE_INTERFACE_Node) == 0) {
        *interface = &h->impl.node;
        return 0;
    }
    return -ENOTSUP;
}

static int impl_handle_clear(struct spa_handle *handle)
{
    struct impl_handle *h = (struct impl_handle *)handle;
    watcher_stop(&h->impl);
    impl_collect_retired(&h->impl);
    eqf_chain *live = atomic_load(&h->impl.chain);
    if (live) {
        eqf_chain_free(live);
        atomic_store(&h->impl.chain, NULL);
    }
    if (h->impl.spare) {
        eqf_chain_free(h->impl.spare);
        h->impl.spare = NULL;
    }
    return 0;
}

static size_t impl_get_size(const struct spa_handle_factory *factory,
                            const struct spa_dict *params)
{
    (void)factory; (void)params;
    return sizeof(struct impl_handle);
}

static int impl_factory_init(const struct spa_handle_factory *factory,
                             struct spa_handle *handle,
                             const struct spa_dict *info,
                             const struct spa_support *support,
                             uint32_t n_support)
{
    (void)factory;
    struct impl_handle *h = (struct impl_handle *)handle;
    struct impl *this;

    h->handle.version = SPA_VERSION_HANDLE;
    h->handle.get_interface = impl_handle_get_interface;
    h->handle.clear = impl_handle_clear;

    this = &h->impl;
    memset(this, 0, sizeof(*this));
    this->node.iface = SPA_INTERFACE_INIT(SPA_TYPE_INTERFACE_Node,
                                          SPA_VERSION_NODE,
                                          &impl_node_methods, this);
    spa_hook_list_init(&this->hooks);
    this->reload_counter = 47;
    this->channels = 2;
    this->rate = 48000;

    this->log = (struct spa_log *)spa_support_find(support, n_support,
                                                   SPA_TYPE_INTERFACE_Log);

    const char *cfg = info ? spa_dict_lookup(info, "config") : NULL;
    if (!cfg && info)
        cfg = spa_dict_lookup(info, "factory.config");
    if (!cfg)
        cfg = getenv("EQFORGE_CONFIG");
    if (cfg) {
        snprintf(this->config_path, sizeof(this->config_path), "%s", cfg);
    } else {
        const char *home = getenv("HOME");
        if (home)
            snprintf(this->config_path, sizeof(this->config_path), "%s/%s",
                     home, DEFAULT_CONFIG_REL);
        else
            this->config_path[0] = '\0';
    }
    eqf_log(this, 3, "config path '%s'", this->config_path);

    atomic_store(&this->cfg_channels, this->channels);
    atomic_store(&this->cfg_rate, this->rate);
    /* Start watching now: after the first format negotiation config_mtime is
     * already current, so a watcher started later can only see subsequent
     * changes - which is what we want, but the node may be created long before
     * it is negotiated. */
    watcher_start(this);
    return 0;
}

static int impl_enum_interface_info(const struct spa_handle_factory *factory,
                                    const struct spa_interface_info **info,
                                    uint32_t *index)
{
    static struct spa_interface_info iface_info = {
        SPA_TYPE_INTERFACE_Node,
    };
    (void)factory;
    if (*index != 0)
        return 0;
    *info = &iface_info;
    (*index)++;
    return 1;
}

static const struct spa_handle_factory impl_factory = {
    SPA_VERSION_HANDLE_FACTORY,
    FACTORY_NAME,
    NULL,
    impl_get_size,
    impl_factory_init,
    impl_enum_interface_info,
};

SPA_EXPORT
int spa_handle_factory_enum(const struct spa_handle_factory **factory,
                            uint32_t *index)
{
    if (factory == NULL || index == NULL)
        return -EINVAL;
    if (*index != 0)
        return 0;
    *factory = &impl_factory;
    (*index)++;
    return 1;
}
