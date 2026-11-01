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
 *
 * SPDX-License-Identifier: MIT
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <spa/buffer/buffer.h>
#include <spa/node/io.h>
#include <spa/node/node.h>
#include <spa/node/utils.h>
#include <spa/param/audio/format-utils.h>
#include <spa/param/latency.h>
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
static uint8_t param_copy[4096];

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
    return b;
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
    dlclose(lib);

    printf("\n%s (%d failures)\n", failures ? "FAILED" : "ALL OK", failures);
    return failures ? 1 : 0;
}
