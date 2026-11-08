/* eqforge-rt: native real-time host for libeqforge.
 *
 * A JACK client (works on PipeWire via pipewire-jack, and on JACK2/jackd)
 * that processes audio through an EQForge chain. The Python control plane
 * talks to it over a Unix socket with line-delimited JSON commands:
 *
 *   {"cmd":"reload","path":"/abs/resolved-dsp.json"}
 *   {"cmd":"bypass","value":true|false}
 *   {"cmd":"connect","outputs":["system:playback_1","system:playback_2"]}
 *   {"cmd":"disconnect"}
 *   {"cmd":"status"}
 *   {"cmd":"quit"}
 *
 * Design notes:
 *  - The audio callback only touches preallocated memory and the chain;
 *    control commands are applied on the socket thread and picked up by the
 *    audio thread through a single-pointer atomic swap of a prepared state
 *    (chain reconfigure itself is RT-tolerant: libeqforge's configure does
 *    bounded work and the core applies a fade).
 *  - Latency reported via jack_set_latency_callback == limiter lookahead.
 *
 * Build: make -C rthost
 *
 * SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-EQForge-Commercial
 */
#define _GNU_SOURCE

#include <errno.h>
#include <getopt.h>
#include <jack/jack.h>
#include <math.h>
#include <poll.h>
#include <pthread.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>

#include "eqforge/eqforge.h"

#define MAX_CHANNELS 8
#define RT_MAX_BLOCK 8192

struct rt_state {
    jack_client_t *client;
    jack_port_t *in[MAX_CHANNELS];
    jack_port_t *out[MAX_CHANNELS];
    uint32_t channels;

    /* Same ownership discipline as the SPA module: the audio thread only ever
     * loads a published pointer, and never allocates or frees. Reloads and
     * sample-rate changes build a replacement chain on the control thread and
     * publish it; the retired chain is freed by the control thread. */
    _Atomic(eqf_chain *) chain;
    /* Retired chains must not be freed until the audio thread provably cannot
     * still be dereferencing them. Tracking an in-flight flag is exact; an
     * earlier version slept for two periods, which is a timing guess and does
     * not establish happens-before for the sanitizers (or for a preempted
     * audio thread). */
    _Atomic bool processing;      /* set by the audio thread around its work */
    eqf_chain *retired[4];
    uint32_t retired_n;
    pthread_mutex_t reload_lock;  /* serialises control-thread rebuilds */
    uint32_t channels_wanted;
    _Atomic uint32_t rate_wanted; /* set by srate_cb, applied off-thread */
    _Atomic bool rebuild_requested;
    _Atomic bool bypass;
    _Atomic bool quit;

    char socket_path[108];
    int listen_fd;
    pthread_t ctl_thread;

    /* Initial config, kept so it can be retried.
     *
     * At login the host is started by systemd as soon as PipeWire is up,
     * while the daemon that publishes active-dsp.json may still be starting.
     * A failed first load used to leave the host running *unconfigured* -
     * i.e. passing audio through untouched - until the daemon happened to
     * push a reload, which is exactly the "the first second has no EQ"
     * behaviour. Keeping the path lets the control thread retry until the
     * file appears. Cleared once a chain is live. */
    char initial_config[512];
    unsigned config_retries;
};

static struct rt_state g_state;

/* ---------------- audio ---------------- */

static int process(jack_nframes_t nframes, void *arg)
{
    struct rt_state *st = arg;
    if (nframes > RT_MAX_BLOCK)
        nframes = RT_MAX_BLOCK;

    float *in[MAX_CHANNELS], *out[MAX_CHANNELS];
    for (uint32_t c = 0; c < st->channels; c++) {
        in[c] = (float *)jack_port_get_buffer(st->in[c], nframes);
        out[c] = (float *)jack_port_get_buffer(st->out[c], nframes);
    }

    if (atomic_load(&st->bypass)) {
        for (uint32_t c = 0; c < st->channels; c++)
            memcpy(out[c], in[c], nframes * sizeof(float));
        return 0;
    }

    atomic_store_explicit(&st->processing, true, memory_order_seq_cst);
    eqf_chain *chain = atomic_load_explicit(&st->chain, memory_order_acquire);
    if (!chain) {
        atomic_store_explicit(&st->processing, false, memory_order_seq_cst);
        for (uint32_t c = 0; c < st->channels; c++)
            memcpy(out[c], in[c], nframes * sizeof(float));
        return 0;
    }
    eqf_chain_process(chain, in, out, nframes);
    atomic_store_explicit(&st->processing, false, memory_order_seq_cst);
    return 0;
}

static void latency_cb(jack_latency_callback_mode_t mode, void *arg)
{
    struct rt_state *st = arg;
    jack_latency_range_t range;
    eqf_chain *chain = atomic_load(&st->chain);
    uint32_t lat = chain ? eqf_chain_latency(chain) : 0;
    range.min = range.max = lat;
    for (uint32_t c = 0; c < st->channels; c++) {
        if (mode == JackCaptureLatency)
            jack_port_set_latency_range(st->in[c], mode, &range);
        else
            jack_port_set_latency_range(st->out[c], mode, &range);
    }
}

/* JACK runs this on its own (non-RT) thread, but eqf_chain_set_sample_rate()
 * frees and reallocates the meters - so it must not run here either: it races
 * with process() and would hand a freed chain to the audio thread. Record the
 * request and let the control thread perform the rebuild. */
static int srate_cb(jack_nframes_t nframes, void *arg)
{
    struct rt_state *st = arg;
    atomic_store(&st->rate_wanted, (uint32_t)nframes);
    atomic_store(&st->rebuild_requested, true);
    fprintf(stderr, "eqforge-rt: sample rate -> %u (applying off-thread)\n",
            nframes);
    return 0;
}

static int bufsize_cb(jack_nframes_t nframes, void *arg)
{
    (void)arg;
    if (nframes > RT_MAX_BLOCK) {
        fprintf(stderr, "eqforge-rt: WARNING buffer %u > %d, clamping\n",
                nframes, RT_MAX_BLOCK);
    }
    return 0;
}

/* ---------------- tiny JSON value extraction (control protocol) ------- */

static const char *json_find_string(const char *json, const char *key,
                                    char *buf, size_t buflen)
{
    char pat[64];
    snprintf(pat, sizeof(pat), "\"%s\"", key);
    const char *p = strstr(json, pat);
    if (!p)
        return NULL;
    p += strlen(pat);
    while (*p == ' ' || *p == ':')
        p++;
    if (*p != '"')
        return NULL;
    p++;
    size_t i = 0;
    while (*p && *p != '"' && i + 1 < buflen)
        buf[i++] = *p++;
    buf[i] = '\0';
    return buf;
}

static int json_find_bool(const char *json, const char *key, int def)
{
    char pat[64];
    snprintf(pat, sizeof(pat), "\"%s\"", key);
    const char *p = strstr(json, pat);
    if (!p)
        return def;
    p += strlen(pat);
    while (*p == ' ' || *p == ':')
        p++;
    if (!strncmp(p, "true", 4))
        return 1;
    if (!strncmp(p, "false", 5))
        return 0;
    return def;
}

/* ---------------- control-thread chain rebuild ---------------- */

/* Build a replacement chain (optionally from a config file), publish it, and
 * keep the previous one in `retired` for the caller to free once the audio
 * thread can no longer be using it. Returns EQF_OK on success. */
static int build_and_publish(struct rt_state *st, const char *config_path,
                             uint32_t rate)
{
    eqf_chain *prev = atomic_load(&st->chain);

    eqf_chain *nc = eqf_chain_new(st->channels_wanted, rate, RT_MAX_BLOCK);
    if (!nc) {
        fprintf(stderr, "eqforge-rt: chain allocation failed\n");
        return EQF_ERR_NO_MEMORY;
    }
    if (config_path) {
        int rc = eqf_chain_configure_file(nc, config_path);
        if (rc != EQF_OK) {
            fprintf(stderr, "eqforge-rt: config '%s' rejected: %s\n",
                    config_path, eqf_result_str(rc));
            eqf_chain_free(nc);
            return rc;
        }
    } else if (prev) {
        /* Rate change with no new file: re-run the previous chain's own
         * serialized config at the new rate so coefficients are recomputed. */
        const char *copy = eqf_chain_current_config(prev);
        if (copy) {
            int rc = eqf_chain_configure_json(nc, copy, strlen(copy));
            if (rc != EQF_OK)
                fprintf(stderr, "eqforge-rt: re-design at %u Hz failed: %s\n",
                        rate, eqf_result_str(rc));
        }
    }
    /* Publish. The audio thread reads `st->chain` once per cycle. */
    atomic_store_explicit(&st->chain, nc, memory_order_release);

    /* Retire the previous chain; reap_retired() frees it once the audio
     * thread is provably outside the callback. Callers hold reload_lock. */
    if (prev && st->retired_n < 4)
        st->retired[st->retired_n++] = prev;
    else if (prev)
        eqf_chain_free(prev);     /* pathological: 4 uncollected rebuilds */
    return EQF_OK;
}

/* Allocate-free of retired chains, safe against a concurrently running audio
 * callback:
 *   1. spin until the audio thread is outside process() (bounded),
 *   2. set `processing` ourselves so the audio thread cannot re-enter and
 *      pick up the old pointer while we free,
 *   3. free, then release.
 * The audio thread only ever sets this flag around its own callback, so the
 * window is at most one callback duration. */
static void reap_retired(struct rt_state *st)
{
    if (st->retired_n == 0)
        return;
    for (int i = 0; i < 20000; i++) {          /* <= ~200 ms */
        if (!atomic_load_explicit(&st->processing, memory_order_seq_cst))
            break;
        struct timespec ts = { 0, 10000L };    /* 10 us */
        nanosleep(&ts, NULL);
    }
    bool was = atomic_exchange(&st->processing, true);
    for (uint32_t i = 0; i < st->retired_n; i++) {
        eqf_chain_free(st->retired[i]);
        st->retired[i] = NULL;
    }
    st->retired_n = 0;
    atomic_store(&st->processing, was);
}

/* ---------------- control socket ---------------- */

static void handle_command(struct rt_state *st, const char *line,
                           char *reply, size_t reply_len)
{
    char cmd[32] = "", path[512] = "";
    json_find_string(line, "cmd", cmd, sizeof(cmd));

    if (!strcmp(cmd, "reload")) {
        if (json_find_string(line, "path", path, sizeof(path))) {
            /* Build the replacement off the audio thread, publish it, and let
             * the reaper free the old graph once no callback can hold it. A
             * rejected config leaves the running chain untouched. */
            pthread_mutex_lock(&st->reload_lock);
            uint32_t rate = atomic_load(&st->rate_wanted);
            if (rate == 0)
                rate = jack_get_sample_rate(st->client);
            int rc = build_and_publish(st, path, rate);
            if (rc == EQF_OK) {
                /* A live chain from an explicit reload supersedes the config
                 * we were launched with: stop retrying it. */
                st->initial_config[0] = '\0';
                st->config_retries = 0;
            }
            pthread_mutex_unlock(&st->reload_lock);
            if (rc == EQF_OK)
                reap_retired(st);
            snprintf(reply, reply_len,
                     "{\"ok\":%s,\"rc\":%d,\"error\":\"%s\"}",
                     rc == EQF_OK ? "true" : "false", rc,
                     rc == EQF_OK ? "" : eqf_result_str(rc));
        } else {
            snprintf(reply, reply_len, "{\"ok\":false,\"error\":\"missing path\"}");
        }
        return;
    }
    if (!strcmp(cmd, "bypass")) {
        int v = json_find_bool(line, "value", 0);
        atomic_store(&st->bypass, (bool)v);
        snprintf(reply, reply_len, "{\"ok\":true,\"bypass\":%s}",
                 v ? "true" : "false");
        return;
    }
    if (!strcmp(cmd, "connect")) {
        /* connect our outputs to the listed playback ports in order */
        const char *p = strstr(line, "\"outputs\"");
        int connected = 0;
        if (p) {
            p = strchr(p, '[');
            if (p) {
                p++;
                for (uint32_t c = 0; c < st->channels && *p && *p != ']'; c++) {
                    char target[256];
                    const char *q = strchr(p, '"');
                    if (!q)
                        break;
                    q++;
                    const char *e = strchr(q, '"');
                    if (!e)
                        break;
                    size_t n = (size_t)(e - q);
                    if (n >= sizeof(target))
                        n = sizeof(target) - 1;
                    memcpy(target, q, n);
                    target[n] = '\0';
                    const char *src = jack_port_name(st->out[c]);
                    if (jack_connect(st->client, src, target) == 0)
                        connected++;
                    p = e + 1;
                }
            }
        }
        snprintf(reply, reply_len, "{\"ok\":true,\"connected\":%d}", connected);
        return;
    }
    if (!strcmp(cmd, "disconnect")) {
        for (uint32_t c = 0; c < st->channels; c++)
            jack_port_disconnect(st->client, st->out[c]);
        snprintf(reply, reply_len, "{\"ok\":true}");
        return;
    }
    if (!strcmp(cmd, "status")) {
        eqf_chain *live = atomic_load(&st->chain);
        /* Consistent copy: reading the live snapshot struct directly races
         * with the audio thread rewriting it. */
        eqf_chain_stats stats_copy;
        eqf_chain_stats *s = NULL;
        if (live && eqf_chain_copy_stats(live, &stats_copy) == EQF_OK)
            s = &stats_copy;
        snprintf(reply, reply_len,
                 "{\"ok\":true,\"version\":\"%s\",\"channels\":%u,"
                 "\"sample_rate\":%u,\"buffer_size\":%u,"
                 "\"latency_frames\":%u,\"bypass\":%s,"
                 "\"momentary_lufs\":%.2f,\"out_peak_db\":%.2f,"
                 "\"limiter_gain_db\":%.2f}",
                 eqf_version_string(), st->channels,
                 jack_get_sample_rate(st->client),
                 jack_get_buffer_size(st->client),
                 live ? eqf_chain_latency(live) : 0,
                 atomic_load(&st->bypass) ? "true" : "false",
                 s ? (double)s->momentary_lufs : -70.0,
                 s ? 20.0 * log10(fmax(s->out_peak[0], 1e-9)) : -70.0,
                 s ? (double)s->limiter_gain_db : 0.0);
        return;
    }
    if (!strcmp(cmd, "quit")) {
        snprintf(reply, reply_len, "{\"ok\":true}");
        atomic_store(&st->quit, true);
        return;
    }
    snprintf(reply, reply_len, "{\"ok\":false,\"error\":\"unknown command\"}");
}

static void *ctl_thread(void *arg)
{
    struct rt_state *st = arg;
    while (!atomic_load(&st->quit)) {
        /* A sample-rate change requested by JACK is applied here, never in the
         * JACK callback: rebuilding reallocates the meters. */
        if (atomic_exchange(&st->rebuild_requested, false)) {
            uint32_t rate = atomic_load(&st->rate_wanted);
            if (rate > 0) {
                pthread_mutex_lock(&st->reload_lock);
                int rc = build_and_publish(st, NULL, rate);
                pthread_mutex_unlock(&st->reload_lock);
                if (rc == EQF_OK)
                    reap_retired(st);
                else
                    fprintf(stderr,
                            "eqforge-rt: rebuild for %u Hz failed: %s\n",
                            rate, eqf_result_str(rc));
            }
        }
        /* Retry a config that was not there yet at start-up (see
         * initial_config). Cheap: one stat/parse per second until it works. */
        if (st->initial_config[0] &&
            atomic_load_explicit(&st->chain, memory_order_acquire) == NULL) {
            /* Every pass through this loop is one 200 ms poll timeout, so a
             * missing config is retried ~5x/s: fast enough that the host is
             * processing before a human can press play, slow enough that a
             * config that never appears costs a few stat() calls. */
            static struct timespec last_try;
            struct timespec now;
            clock_gettime(CLOCK_MONOTONIC, &now);
            long since_ms = (now.tv_sec - last_try.tv_sec) * 1000L +
                            (now.tv_nsec - last_try.tv_nsec) / 1000000L;
            if (since_ms >= 200) {
                last_try = now;
                pthread_mutex_lock(&st->reload_lock);
                uint32_t rate = atomic_load(&st->rate_wanted);
                if (rate == 0)
                    rate = jack_get_sample_rate(st->client);
                int rc = build_and_publish(st, st->initial_config, rate);
                pthread_mutex_unlock(&st->reload_lock);
                if (rc == EQF_OK) {
                    fprintf(stderr, "eqforge-rt: config '%s' loaded\n",
                            st->initial_config);
                    st->initial_config[0] = '\0';
                    reap_retired(st);
                } else if (st->config_retries++ % 150 == 0) {
                    fprintf(stderr,
                            "eqforge-rt: waiting for config '%s' (%s)\n",
                            st->initial_config, eqf_result_str(rc));
                }
            }
        }
        struct pollfd pfd = { .fd = st->listen_fd, .events = POLLIN };
        int pr = poll(&pfd, 1, 200);
        if (pr <= 0)
            continue;
        int fd = accept(st->listen_fd, NULL, NULL);
        if (fd < 0)
            continue;
        char buf[4096];
        ssize_t n = recv(fd, buf, sizeof(buf) - 1, 0);
        if (n > 0) {
            buf[n] = '\0';
            char *line = buf;
            char *nl = strchr(buf, '\n');
            if (nl)
                *nl = '\0';
            char reply[1024] = "{\"ok\":false}";
            handle_command(st, line, reply, sizeof(reply));
            ssize_t w = send(fd, reply, strlen(reply), MSG_NOSIGNAL);
            (void)w;
        }
        close(fd);
    }
    return NULL;
}

static void on_jack_shutdown(void *arg)
{
    struct rt_state *st = arg;
    fprintf(stderr, "eqforge-rt: JACK server shutdown\n");
    atomic_store(&st->quit, true);
}

/* ---------------- main ---------------- */

static void usage(const char *argv0)
{
    fprintf(stderr,
        "Usage: %s [options]\n"
        "  -c, --channels N     channel pairs (default 2, max %d)\n"
        "  -n, --name NAME      JACK client name (default eqforge)\n"
        "  -s, --socket PATH    control socket (default $XDG_RUNTIME_DIR/eqforge/rt.sock)\n"
        "  -f, --config PATH    initial resolved dsp-config JSON\n"
        "  -h, --help\n", argv0, MAX_CHANNELS);
}

int main(int argc, char **argv)
{
    struct rt_state *st = &g_state;
    memset(st, 0, sizeof(*st));
    st->channels = 2;
    st->channels_wanted = 2;
    pthread_mutex_init(&st->reload_lock, NULL);
    const char *client_name = "eqforge";
    const char *config = NULL;

    static struct option opts[] = {
        { "channels", required_argument, NULL, 'c' },
        { "name", required_argument, NULL, 'n' },
        { "socket", required_argument, NULL, 's' },
        { "config", required_argument, NULL, 'f' },
        { "help", no_argument, NULL, 'h' },
        { NULL, 0, NULL, 0 },
    };
    int opt;
    while ((opt = getopt_long(argc, argv, "c:n:s:f:h", opts, NULL)) != -1) {
        switch (opt) {
        case 'c': st->channels = (uint32_t)atoi(optarg); break;
        case 'n': client_name = optarg; break;
        case 's': snprintf(st->socket_path, sizeof(st->socket_path), "%s", optarg); break;
        case 'f': config = optarg; break;
        default: usage(argv[0]); return opt == 'h' ? 0 : 1;
        }
    }
    if (st->channels == 0 || st->channels > MAX_CHANNELS) {
        fprintf(stderr, "eqforge-rt: channels must be 1..%d\n", MAX_CHANNELS);
        return 1;
    }
    st->channels_wanted = st->channels;

    if (st->socket_path[0] == '\0') {
        const char *xdg = getenv("XDG_RUNTIME_DIR");
        if (!xdg || !*xdg)
            xdg = "/tmp";
        snprintf(st->socket_path, sizeof(st->socket_path),
                 "%s/eqforge/rt.sock", xdg);
    }

    jack_status_t jstatus;
    st->client = jack_client_open(client_name, JackNoStartServer, &jstatus);
    if (!st->client) {
        fprintf(stderr, "eqforge-rt: cannot open JACK client (%d). Is PipeWire/JACK running?\n",
                (int)jstatus);
        return 2;
    }

    jack_set_process_callback(st->client, process, st);
    jack_set_latency_callback(st->client, latency_cb, st);
    jack_on_shutdown(st->client, on_jack_shutdown, st);
    jack_set_sample_rate_callback(st->client, srate_cb, st);
    jack_set_buffer_size_callback(st->client, bufsize_cb, st);

    for (uint32_t c = 0; c < st->channels; c++) {
        char nm[32];
        snprintf(nm, sizeof(nm), "in_%u", c + 1);
        st->in[c] = jack_port_register(st->client, nm, JACK_DEFAULT_AUDIO_TYPE,
                                       JackPortIsInput, 0);
        snprintf(nm, sizeof(nm), "out_%u", c + 1);
        st->out[c] = jack_port_register(st->client, nm, JACK_DEFAULT_AUDIO_TYPE,
                                        JackPortIsOutput, 0);
        if (!st->in[c] || !st->out[c]) {
            fprintf(stderr, "eqforge-rt: port registration failed\n");
            return 2;
        }
    }

    {
        uint32_t rate = jack_get_sample_rate(st->client);
        atomic_store(&st->rate_wanted, rate);
        if (config)
            snprintf(st->initial_config, sizeof(st->initial_config), "%s",
                     config);
        int rc = build_and_publish(st, config, rate);
        if (rc != EQF_OK && !config) {
            /* No initial config is legitimate (pass-through), but a failure to
             * allocate the chain is not. */
            fprintf(stderr, "eqforge-rt: chain allocation failed\n");
            return 2;
        }
    }

    /* control socket */
    if (strlen(st->socket_path) >= sizeof(((struct sockaddr_un *)0)->sun_path)) {
        fprintf(stderr, "eqforge-rt: socket path too long: %s\n", st->socket_path);
        return 1;
    }
    char dir[280];
    snprintf(dir, sizeof(dir), "%s", st->socket_path);
    char *slash = strrchr(dir, '/');
    if (slash) {
        *slash = '\0';
        mkdir(dir, 0755);
    }
    unlink(st->socket_path);
    st->listen_fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    struct sockaddr_un addr;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    snprintf(addr.sun_path, sizeof(addr.sun_path), "%s", st->socket_path);
    if (bind(st->listen_fd, (struct sockaddr *)&addr, sizeof(addr)) < 0 ||
        listen(st->listen_fd, 4) < 0) {
        fprintf(stderr, "eqforge-rt: cannot bind %s: %s\n", st->socket_path,
                strerror(errno));
        return 2;
    }
    chmod(st->socket_path, 0600);
    pthread_create(&st->ctl_thread, NULL, ctl_thread, st);

    if (jack_activate(st->client)) {
        fprintf(stderr, "eqforge-rt: cannot activate client\n");
        return 2;
    }

    printf("{\"ok\":true,\"client\":\"%s\",\"channels\":%u,\"sample_rate\":%u,"
           "\"buffer_size\":%u,\"socket\":\"%s\",\"lib\":\"%s\"}\n",
           client_name, st->channels, jack_get_sample_rate(st->client),
           jack_get_buffer_size(st->client), st->socket_path,
           eqf_version_string());
    fflush(stdout);

    while (!atomic_load(&st->quit))
        sleep(1);

    jack_deactivate(st->client);
    jack_client_close(st->client);
    close(st->listen_fd);
    unlink(st->socket_path);
    pthread_join(st->ctl_thread, NULL);
    /* Stop the audio thread first, then release both the live and the retired
     * chain. (Dropping the live pointer without freeing it leaks the whole
     * graph - caught by LeakSanitizer, not by any functional check.) */
    eqf_chain *live = atomic_exchange(&st->chain, NULL);
    eqf_chain_free(live);
    for (uint32_t i = 0; i < st->retired_n; i++)
        eqf_chain_free(st->retired[i]);
    pthread_mutex_destroy(&st->reload_lock);
    return 0;
}
