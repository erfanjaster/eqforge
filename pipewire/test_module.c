/* EQForge PipeWire module smoke test.
 *
 * Exercises the SPA plugin exactly like a PipeWire host would, without
 * needing a running daemon:
 *   1. dlopen the module, enumerate handle factories
 *   2. instantiate the node via the factory
 *   3. negotiate F32 stereo @48k via set_param(SPA_PARAM_Format)
 *   4. add ports, set io areas, provide buffers via port_use_buffers
 *   5. process blocks of a 1 kHz sine through an EQ + limiter config
 *   6. verify DSP actually happened (limiter ceiling respected)
 *   7. rewrite the config file and verify the node hot-reloads it
 *      (the watcher thread must stage a new chain without touching process())
 *
 * SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-EQForge-Commercial
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include <spa/buffer/buffer.h>
#include <spa/node/io.h>
#include <spa/node/node.h>
#include <spa/node/utils.h>
#include <spa/param/audio/format-utils.h>
#include <spa/param/latency.h>
#include <spa/param/latency-utils.h>
#include <spa/param/props.h>
#include <spa/param/latency-utils.h>
#include <spa/pod/builder.h>
#include <spa/pod/parser.h>
#include <spa/support/plugin.h>
#include <spa/utils/result.h>

#define FRAMES 256
#define CHANNELS 2

static int failures = 0;
#define CHECK(cond, msg)                                                       \
    do {                                                                       \
        if (!(cond)) {                                                         \
            printf("  FAIL: %s (line %d)\n", msg, __LINE__);                   \
            failures++;                                                        \
        } else {                                                               \
            printf("  ok: %s\n", msg);                                         \
        }                                                                      \
    } while (0)

typedef int (*enum_func_t)(const struct spa_handle_factory **factory,
                           uint32_t *index);

static struct spa_result_node_params last_param;
/* spa_pod requires 8-byte alignment (it holds int64/uint64 members). A plain
 * uint8_t array is only 1-byte aligned, which UBSan flags as a misaligned
 * store as soon as the pod is built into it. */
static _Alignas(8) uint8_t param_copy[4096];

static void on_result(void *data, int seq, int res, uint32_t type,
                      const void *result)
{
    (void)data; (void)seq; (void)res;
    if (type == SPA_RESULT_TYPE_NODE_PARAMS) {
        const struct spa_result_node_params *p = result;
        if (p->param && SPA_POD_SIZE(p->param) <= sizeof(param_copy)) {
            memcpy(param_copy, p->param, SPA_POD_SIZE(p->param));
            last_param.param = (struct spa_pod *)param_copy;
        }
    }
}

static const struct spa_node_events node_events = {
    SPA_VERSION_NODE_EVENTS,
    .result = on_result,
};

/* Harness bookkeeping: leak checking is only meaningful if the test frees its
 * own fixtures, otherwise every run reports the harness's allocations. */
static struct spa_buffer *g_buffers[16];
static uint32_t g_n_buffers;

static struct spa_buffer *make_buffer(uint32_t blocks, uint32_t size)
{
    struct spa_buffer *b = calloc(1, sizeof(struct spa_buffer) +
                                  blocks * sizeof(struct spa_data) +
                                  blocks * sizeof(struct spa_chunk));
    b->n_datas = blocks;
    b->datas = (struct spa_data *)(b + 1);
    for (uint32_t i = 0; i < blocks; i++) {
        b->datas[i].type = 4; /* SPA_DATA_MemPtr */
        b->datas[i].maxsize = size;
        b->datas[i].data = calloc(1, size);
        b->datas[i].chunk = (struct spa_chunk *)(b->datas + blocks) + i;
        b->datas[i].chunk->stride = 4;
    }
    if (g_n_buffers < 16)
        g_buffers[g_n_buffers++] = b;
    return b;
}

static void free_buffers(void)
{
    for (uint32_t i = 0; i < g_n_buffers; i++) {
        struct spa_buffer *b = g_buffers[i];
        for (uint32_t j = 0; j < b->n_datas; j++)
            free(b->datas[j].data);
        free(b);
    }
    g_n_buffers = 0;
}

int main(int argc, char **argv)
{
    const char *so = argc > 1 ? argv[1] : "build/libeqforge-filter.so";
    const char *cfg = argc > 2 ? argv[2] : "/tmp/eqforge-test-dsp.json";

    /* write a test dsp config: +12 dB @1k peak, limiter ceiling -3 dB */
    FILE *f = fopen(cfg, "w");
    if (!f) { perror("config"); return 2; }
    fprintf(f, "{\"preamp_db\":0,\"eq\":{\"filters\":["
               "{\"type\":\"peak\",\"freq\":1000,\"gain_db\":12,\"q\":1.0}]},"
               "\"limiter\":{\"enabled\":true,\"ceiling_db\":-3.0,"
               "\"attack_ms\":1,\"release_ms\":40,\"lookahead_ms\":2}}");
    fclose(f);
    setenv("EQFORGE_CONFIG", cfg, 1);

    printf("eqforge SPA module smoke test\n");

    void *lib = dlopen(so, RTLD_NOW);
    CHECK(lib != NULL, "dlopen module");
    if (!lib) { printf("%s\n", dlerror()); return 1; }

    enum_func_t enum_fn = (enum_func_t)dlsym(lib, "spa_handle_factory_enum");
    CHECK(enum_fn != NULL, "spa_handle_factory_enum symbol");
    if (!enum_fn)
        return 1;

    const struct spa_handle_factory *factory = NULL;
    uint32_t idx = 0;
    int r = enum_fn(&factory, &idx);
    CHECK(r == 1 && factory != NULL, "factory enumeration");
    CHECK(factory && strcmp(factory->name, "eqforge.filter") == 0,
          "factory name is eqforge.filter");
    if (!factory)
        return 1;

    size_t sz = factory->get_size(factory, NULL);
    struct spa_handle *handle = calloc(1, sz);
    CHECK(handle != NULL, "allocate handle");
    r = factory->init(factory, handle, NULL, NULL, 0);
    CHECK(r == 0, "factory init");

    struct spa_node *node = NULL;
    r = spa_handle_get_interface(handle, SPA_TYPE_INTERFACE_Node,
                                 (void **)&node);
    CHECK(r == 0 && node != NULL, "get Node interface");
    if (!node)
        return 1;

    /* --- props before the format is known ------------------------------
     * A host may push SPA_PROP_mute through the Props parameter before it has
     * negotiated a format, i.e. while the node has no sample rate and no
     * chain. That must be accepted and remembered, not rejected: a manager
     * that gets -EINVAL here drops the node. The bypass flag is only applied
     * by process(), which cannot run before a format exists, so the ordering
     * is safe by construction - assert it rather than assume it. */
    {
        uint8_t ebuf[256];
        struct spa_pod_builder eb = SPA_POD_BUILDER_INIT(ebuf, sizeof(ebuf));
        struct spa_pod_frame efr;
        spa_pod_builder_push_object(&eb, &efr, SPA_TYPE_OBJECT_Props,
                                    SPA_PARAM_Props);
        spa_pod_builder_add(&eb, SPA_PROP_mute, SPA_POD_Bool(true), 0);
        struct spa_pod *ep = spa_pod_builder_pop(&eb, &efr);
        int erc = spa_node_set_param(node, SPA_PARAM_Props, 0, ep);
        CHECK(erc == 0, "Props(mute) accepted before any format is negotiated");

        /* and it must be reversible, still before the format */
        struct spa_pod_builder eb2 = SPA_POD_BUILDER_INIT(ebuf, sizeof(ebuf));
        struct spa_pod_frame efr2;
        spa_pod_builder_push_object(&eb2, &efr2, SPA_TYPE_OBJECT_Props,
                                    SPA_PARAM_Props);
        spa_pod_builder_add(&eb2, SPA_PROP_mute, SPA_POD_Bool(false), 0);
        struct spa_pod *ep2 = spa_pod_builder_pop(&eb2, &efr2);
        CHECK(spa_node_set_param(node, SPA_PARAM_Props, 0, ep2) == 0,
              "Props(mute=false) also accepted before format");
    }

    struct spa_hook listener;
    memset(&listener, 0, sizeof(listener));
    r = spa_node_add_listener(node, &listener, &node_events, NULL);
    CHECK(r == 0, "add_listener");

    /* enumerate formats */
    memset(&last_param, 0, sizeof(last_param));
    r = spa_node_enum_params(node, 0, SPA_PARAM_EnumFormat, 0, 1, NULL);
    CHECK(r == 1 && last_param.param != NULL, "EnumFormat returns a format");

    /* negotiate F32 stereo 48k */
    uint8_t fbuf[1024];
    struct spa_pod_builder fb = SPA_POD_BUILDER_INIT(fbuf, sizeof(fbuf));
    struct spa_audio_info_raw raw;
    memset(&raw, 0, sizeof(raw));
    raw.format = SPA_AUDIO_FORMAT_F32;
    raw.channels = CHANNELS;
    raw.rate = 48000;
    raw.position[0] = 1; /* FL */
    raw.position[1] = 2; /* FR */
    struct spa_pod *fmt = spa_format_audio_raw_build(&fb, SPA_PARAM_Format,
                                                     &raw);
    CHECK(fmt != NULL, "build format pod");
    r = spa_node_set_param(node, SPA_PARAM_Format, 0, fmt);
    CHECK(r == 0, "set_param Format F32/2ch/48k");

    /* add ports */
    r = spa_node_add_port(node, SPA_DIRECTION_INPUT, 0, NULL);
    CHECK(r == 0, "add input port");
    r = spa_node_add_port(node, SPA_DIRECTION_OUTPUT, 0, NULL);
    CHECK(r == 0, "add output port");

    /* io areas */
    struct spa_io_buffers io_in = SPA_IO_BUFFERS_INIT;
    struct spa_io_buffers io_out = SPA_IO_BUFFERS_INIT;
    r = spa_node_port_set_io(node, SPA_DIRECTION_INPUT, 0, SPA_IO_Buffers,
                             &io_in, sizeof(io_in));
    CHECK(r == 0, "port_set_io input");
    r = spa_node_port_set_io(node, SPA_DIRECTION_OUTPUT, 0, SPA_IO_Buffers,
                             &io_out, sizeof(io_out));
    CHECK(r == 0, "port_set_io output");

    /* buffers: planar (one data per channel) */
    uint32_t bufsize = FRAMES * sizeof(float);
    struct spa_buffer *inb[4], *outb[4];
    for (int i = 0; i < 4; i++) {
        inb[i] = make_buffer(CHANNELS, bufsize);
        outb[i] = make_buffer(CHANNELS, bufsize);
    }
    r = spa_node_port_use_buffers(node, SPA_DIRECTION_INPUT, 0, 0, inb, 4);
    CHECK(r == 0, "port_use_buffers input");
    r = spa_node_port_use_buffers(node, SPA_DIRECTION_OUTPUT, 0, 0, outb, 4);
    CHECK(r == 0, "port_use_buffers output");

    /* fill input with a loud 1 kHz sine (planar, stride=4 => planar detect) */
    for (int i = 0; i < 4; i++) {
        for (uint32_t c = 0; c < CHANNELS; c++) {
            float *p = inb[i]->datas[c].data;
            for (uint32_t j = 0; j < FRAMES; j++) {
                double t = (double)(i * FRAMES + j) / 48000.0;
                p[j] = (float)(0.9 * sin(2.0 * M_PI * 1000.0 * t));
            }
            inb[i]->datas[c].chunk->offset = 0;
            inb[i]->datas[c].chunk->size = bufsize;
            inb[i]->datas[c].chunk->stride = 4;
        }
    }

    /* process without output buffer: expect in-place mode */
    io_in.buffer_id = 0;
    io_out.buffer_id = SPA_ID_INVALID;
    r = spa_node_process(node);
    CHECK(r == SPA_STATUS_HAVE_DATA, "process (in-place) returns HAVE_DATA");
    CHECK(io_out.buffer_id == 0, "in-place: output reuses input buffer id");

    /* process with dedicated output buffer */
    io_in.buffer_id = 1;
    io_out.buffer_id = 2;
    r = spa_node_process(node);
    CHECK(r == SPA_STATUS_HAVE_DATA, "process (dedicated buffer) HAVE_DATA");

    /* verify DSP: +12dB @1k then limiter at -3 dBTP => output peak <= -3dB
     * (after the fade-in period; feed many blocks first) */
    for (int i = 0; i < 200; i++) {
        io_in.buffer_id = i & 3;
        io_out.buffer_id = 2; /* dedicated output: keeps inputs pristine */
        spa_node_process(node);
    }
    struct spa_buffer *last = outb[2];
    float peak = 0.0f;
    for (uint32_t c = 0; c < CHANNELS; c++) {
        float *p = last->datas[c].data;
        for (uint32_t j = 0; j < FRAMES; j++)
            if (fabsf(p[j]) > peak)
                peak = fabsf(p[j]);
    }
    float ceiling = (float)pow(10.0, -3.0 / 20.0);
    printf("  output peak: %.4f (ceiling %.4f)\n", peak, ceiling);
    CHECK(peak <= ceiling + 1e-5f, "limiter holds ceiling through the node");
    CHECK(peak > ceiling * 0.5f, "signal actually present (not silence)");

    /* --- hot reload: rewrite the config, wait for the watcher, reprocess --- */
    f = fopen(cfg, "w");
    CHECK(f != NULL, "reopen config for hot reload");
    if (f) {
        /* +12 dB @1k removed, limiter ceiling raised to -1 dB: output peak must
         * rise from 0.707 to ~0.891 if the new config is actually loaded. */
        fprintf(f, "{\"preamp_db\":0,\"eq\":{\"filters\":[]},"
                   "\"limiter\":{\"enabled\":true,\"ceiling_db\":-1.0,"
                   "\"attack_ms\":1,\"release_ms\":40,\"lookahead_ms\":2}}");
        fclose(f);
        /* the watcher polls at 2 Hz; give it time plus margin.
         * NOTE: tv_nsec must stay < 1e9 - a "1.5 s" value of 1500000000L is
         * rejected by nanosleep() with EINVAL, i.e. it waits zero seconds. */
        struct timespec ts = { .tv_sec = 1, .tv_nsec = 500000000L };
        CHECK(nanosleep(&ts, NULL) == 0, "hot reload wait sleeps fully");
        for (int i = 0; i < 400; i++) {
            io_in.buffer_id = i & 3;
            io_out.buffer_id = 2;
            spa_node_process(node);
        }
        float new_peak = 0.0f;
        for (uint32_t c = 0; c < CHANNELS; c++) {
            float *p = outb[2]->datas[c].data;
            for (uint32_t j = 0; j < FRAMES; j++)
                if (fabsf(p[j]) > new_peak)
                    new_peak = fabsf(p[j]);
        }
        float new_ceiling = (float)pow(10.0, -1.0 / 20.0);
        printf("  after reload peak: %.4f (new ceiling %.4f)\n",
               new_peak, new_ceiling);
        CHECK(new_peak > ceiling * 1.05f,
              "hot reload picked up the new (louder) config");
        CHECK(new_peak <= new_ceiling + 1e-5f,
              "hot-reloaded limiter still holds its ceiling");

        float tight_peak = 0.0f;
        /* Reverse direction: a reload must also be able to *tighten* the
         * ceiling. Without this the first check could be satisfied by any
         * accidental level change rather than by reading the new file. */
        f = fopen(cfg, "w");
        if (f) {
            fprintf(f, "{\"preamp_db\":0,\"eq\":{\"filters\":[]},"
                       "\"limiter\":{\"enabled\":true,\"ceiling_db\":-12.0,"
                       "\"attack_ms\":1,\"release_ms\":40,\"lookahead_ms\":2}}");
            fclose(f);
            struct timespec ts2 = { .tv_sec = 1, .tv_nsec = 500000000L };
            nanosleep(&ts2, NULL);
            for (int i = 0; i < 400; i++) {
                io_in.buffer_id = i & 3;
                io_out.buffer_id = 2;
                spa_node_process(node);
            }
            for (uint32_t c = 0; c < CHANNELS; c++) {
                float *p = outb[2]->datas[c].data;
                for (uint32_t j = 0; j < FRAMES; j++)
                    if (fabsf(p[j]) > tight_peak)
                        tight_peak = fabsf(p[j]);
            }
            float tight_ceiling = (float)pow(10.0, -12.0 / 20.0);
            printf("  after second reload peak: %.4f (ceiling %.4f)\n",
                   tight_peak, tight_ceiling);
            CHECK(tight_peak <= tight_ceiling + 1e-5f,
                  "second reload tightened the ceiling");
            CHECK(tight_peak < new_peak * 0.9f,
                  "second reload lowered the output level");
        }

        /* Negative case: a syntactically broken config must be rejected and
         * leave the previous (working) chain in place. */
        int before = failures;
        f = fopen(cfg, "w");
        if (f) {
            fprintf(f, "{ this is not json ");
            fclose(f);
            struct timespec ts3 = { .tv_sec = 1, .tv_nsec = 200000000L };
            nanosleep(&ts3, NULL);
            for (int i = 0; i < 200; i++) {
                io_in.buffer_id = i & 3;
                io_out.buffer_id = 2;
                spa_node_process(node);
            }
            float still = 0.0f;
            for (uint32_t c = 0; c < CHANNELS; c++) {
                float *p = outb[2]->datas[c].data;
                for (uint32_t j = 0; j < FRAMES; j++)
                    if (fabsf(p[j]) > still)
                        still = fabsf(p[j]);
            }
            printf("  after bad config peak: %.4f\n", still);
            CHECK(fabsf(still - tight_peak) < 1e-4f,
                  "invalid config rejected, previous chain still active");
            CHECK(failures == before,
                  "invalid config produced no processing failure");
        }
    }

    /* --- mute prop must actually bypass the processing --- */
    {
        uint8_t pbuf[512];
        struct spa_pod_builder pb = SPA_POD_BUILDER_INIT(pbuf, sizeof(pbuf));
        struct spa_pod_frame fr;
        spa_pod_builder_push_object(&pb, &fr, SPA_TYPE_OBJECT_Props,
                                    SPA_PARAM_Props);
        spa_pod_builder_add(&pb, SPA_PROP_mute, SPA_POD_Bool(true), 0);
        struct spa_pod *props = spa_pod_builder_pop(&pb, &fr);
        r = spa_node_set_param(node, SPA_PARAM_Props, 0, props);
        CHECK(r == 0, "set_param Props(mute=true) accepted");

        for (int i = 0; i < 200; i++) {
            io_in.buffer_id = i & 3;
            io_out.buffer_id = 2;
            spa_node_process(node);
        }
        float muted_peak = 0.0f;
        for (uint32_t c = 0; c < CHANNELS; c++) {
            float *p = outb[2]->datas[c].data;
            for (uint32_t j = 0; j < FRAMES; j++)
                if (fabsf(p[j]) > muted_peak)
                    muted_peak = fabsf(p[j]);
        }
        printf("  peak while muted: %.4f\n", muted_peak);
        /* bypass passes input through: the source is 0.9 amplitude, so a muted
         * node is LOUDER than the limited one, not silent. */
        CHECK(muted_peak > 0.85f, "mute bypasses processing (signal passes)");

        /* unmute and confirm the limiter is back in circuit */
        struct spa_pod_builder pb2 = SPA_POD_BUILDER_INIT(pbuf, sizeof(pbuf));
        struct spa_pod_frame fr2;
        spa_pod_builder_push_object(&pb2, &fr2, SPA_TYPE_OBJECT_Props,
                                    SPA_PARAM_Props);
        spa_pod_builder_add(&pb2, SPA_PROP_mute, SPA_POD_Bool(false), 0);
        struct spa_pod *props2 = spa_pod_builder_pop(&pb2, &fr2);
        CHECK(spa_node_set_param(node, SPA_PARAM_Props, 0, props2) == 0,
              "set_param Props(mute=false) accepted");
        for (int i = 0; i < 400; i++) {
            io_in.buffer_id = i & 3;
            io_out.buffer_id = 2;
            spa_node_process(node);
        }
        float back = 0.0f;
        for (uint32_t c = 0; c < CHANNELS; c++) {
            float *p = outb[2]->datas[c].data;
            for (uint32_t j = 0; j < FRAMES; j++)
                if (fabsf(p[j]) > back)
                    back = fabsf(p[j]);
        }
        float tight = (float)pow(10.0, -12.0 / 20.0);
        printf("  peak after unmute: %.4f (ceiling %.4f)\n", back, tight);
        CHECK(back <= tight + 1e-4f, "unmute restores processing");
    }

    /* Setting the read-only latency param must be refused, not silently
     * accepted: this node derives its latency from the limiter. */
    {
        uint8_t lbuf[512];
        struct spa_pod_builder lb = SPA_POD_BUILDER_INIT(lbuf, sizeof(lbuf));
        struct spa_latency_info li =
            SPA_LATENCY_INFO(SPA_DIRECTION_OUTPUT, .min_ns = 0, .max_ns = 0);
        struct spa_pod *lp = spa_latency_build(&lb, SPA_PARAM_Latency, &li);
        r = spa_node_set_param(node, SPA_PARAM_Latency, 0, lp);
        CHECK(r < 0, "set_param Latency is refused (read-only)");
    }

    /* latency param reflects limiter lookahead (2 ms @48k = 96 frames) */
    memset(&last_param, 0, sizeof(last_param));
    r = spa_node_enum_params(node, 0, SPA_PARAM_Latency, 0, 1, NULL);
    CHECK(r == 1, "Latency param enumerable");
    if (last_param.param) {
        struct spa_latency_info lat;
        memset(&lat, 0, sizeof(lat));
        if (spa_latency_parse(last_param.param, &lat) == 0) {
            printf("  latency: %lu ns\n", (unsigned long)lat.min_ns);
            CHECK(lat.min_ns == 96ull * 1000000000ull / 48000ull,
                  "latency == lookahead (96 frames)");
        }
    }

    spa_hook_remove(&listener);
    handle->clear(handle);
    free(handle);
    free_buffers();
    dlclose(lib);

    printf("\n%s (%d failures)\n", failures ? "FAILED" : "ALL OK", failures);
    return failures ? 1 : 0;
}
