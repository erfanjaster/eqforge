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

#include <errno.h>
#include <limits.h>
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

#define NAME "eqforge"
#define FACTORY_NAME "eqforge.filter"
#define DEFAULT_CONFIG_REL ".config/eqforge/active-dsp.json"
#define MAX_FRAMES 8192
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

    uint8_t pod_buffer[8192];

    eqf_chain *chain;
    char config_path[PATH_MAX];
    time_t config_mtime;
    long reload_counter;

    uint32_t channels;
    uint32_t rate;
};

static void impl_update_config(struct impl *this, bool force);

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
        if (this->chain && this->rate > 0) {
            uint32_t lat = eqf_chain_latency(this->chain);
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

    if (this->chain) {
        eqf_chain_free(this->chain);
        this->chain = NULL;
    }
    this->chain = eqf_chain_new(this->channels, this->rate, MAX_FRAMES);
    if (!this->chain) {
        if (this->log)
            spa_log_error(this->log, NAME ": failed to allocate chain");
        return -ENOMEM;
    }
    impl_update_config(this, true);
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
    case SPA_PARAM_Props:
        return 0;
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
        if (this->chain && this->rate > 0) {
            uint32_t lat = eqf_chain_latency(this->chain);
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

static void impl_update_config(struct impl *this, bool force)
{
    if (!this->chain || this->config_path[0] == '\0')
        return;

    struct stat st;
    if (stat(this->config_path, &st) != 0)
        return;
    if (!force && st.st_mtime == this->config_mtime)
        return;
    this->config_mtime = st.st_mtime;

    int rc = eqf_chain_configure_file(this->chain, this->config_path);
    if (rc != EQF_OK && this->log) {
        const char *err = eqf_chain_last_error(this->chain);
        spa_log_error(this->log, NAME ": config '%s' rejected (%s): %s",
                      this->config_path, eqf_result_str(rc),
                      err ? err : "no detail");
    } else if (rc == EQF_OK && this->log) {
        spa_log_info(this->log, NAME ": loaded config '%s'", this->config_path);
    }
}

static int process_buffers(struct impl *this, struct spa_buffer *ib,
                           struct spa_buffer *ob)
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
        if (this->chain) {
            eqf_chain_process(this->chain, ip, op, frames);
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
        if (this->chain) {
            float tmp_in[EQF_MAX_CHANNELS][MAX_FRAMES];
            float tmp_out[EQF_MAX_CHANNELS][MAX_FRAMES];
            float *ip[EQF_MAX_CHANNELS], *op[EQF_MAX_CHANNELS];
            for (uint32_t c = 0; c < ch; c++) {
                ip[c] = tmp_in[c];
                op[c] = tmp_out[c];
            }
            for (uint32_t i = 0; i < frames; i++)
                for (uint32_t c = 0; c < ch; c++)
                    tmp_in[c][i] = src[i * ch + c];
            eqf_chain_process(this->chain, ip, op, frames);
            for (uint32_t i = 0; i < frames; i++)
                for (uint32_t c = 0; c < ch; c++)
                    dst[i * ch + c] = tmp_out[c][i];
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

    /* periodic config reload (~1 Hz at quantum 1024/48k) */
    if (--this->reload_counter <= 0) {
        this->reload_counter = 47;
        impl_update_config(this, false);
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
        if (process_buffers(this, inb, out->buffers[obi->buffer_id]) < 0)
            obi->buffer_id = SPA_ID_INVALID;
        return SPA_STATUS_HAVE_DATA;
    }

    /* host provided no output buffer id: process in place and forward the
     * input buffer */
    if (process_buffers(this, inb, inb) < 0) {
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
    if (h->impl.chain) {
        eqf_chain_free(h->impl.chain);
        h->impl.chain = NULL;
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
    if (this->log && this->config_path[0])
        spa_log_info(this->log, NAME ": config path '%s'", this->config_path);

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
