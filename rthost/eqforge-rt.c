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
 * SPDX-License-Identifier: MIT
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
#include <unistd.h>

#include "eqforge/eqforge.h"

#define MAX_CHANNELS 8
#define RT_MAX_BLOCK 8192

struct rt_state {
    jack_client_t *client;
    jack_port_t *in[MAX_CHANNELS];
    jack_port_t *out[MAX_CHANNELS];
    uint32_t channels;

    eqf_chain *chain;
    pthread_mutex_t chain_lock;   /* held only around reconfigure, not audio */
    _Atomic bool bypass;
    _Atomic bool quit;

    char socket_path[108];
    int listen_fd;
    pthread_t ctl_thread;
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

    eqf_chain *chain = st->chain; /* stable pointer; reconfigure is in-place */
    if (!chain) {
        for (uint32_t c = 0; c < st->channels; c++)
            memcpy(out[c], in[c], nframes * sizeof(float));
        return 0;
    }
    eqf_chain_process(chain, in, out, nframes);
    return 0;
}

static void latency_cb(jack_latency_callback_mode_t mode, void *arg)
{
    struct rt_state *st = arg;
    jack_latency_range_t range;
    uint32_t lat = st->chain ? eqf_chain_latency(st->chain) : 0;
    range.min = range.max = lat;
    for (uint32_t c = 0; c < st->channels; c++) {
        if (mode == JackCaptureLatency)
            jack_port_set_latency_range(st->in[c], mode, &range);
        else
            jack_port_set_latency_range(st->out[c], mode, &range);
    }
}

static int srate_cb(jack_nframes_t nframes, void *arg)
{
    struct rt_state *st = arg;
    if (st->chain)
        eqf_chain_set_sample_rate(st->chain, nframes);
    fprintf(stderr, "eqforge-rt: sample rate -> %u\n", nframes);
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

/* ---------------- control socket ---------------- */

static void handle_command(struct rt_state *st, const char *line,
                           char *reply, size_t reply_len)
{
    char cmd[32] = "", path[512] = "";
    json_find_string(line, "cmd", cmd, sizeof(cmd));

    if (!strcmp(cmd, "reload")) {
        if (json_find_string(line, "path", path, sizeof(path))) {
            pthread_mutex_lock(&st->chain_lock);
            if (!st->chain)
                st->chain = eqf_chain_new(st->channels,
                                          jack_get_sample_rate(st->client),
                                          RT_MAX_BLOCK);
            int rc = st->chain ? eqf_chain_configure_file(st->chain, path)
                               : EQF_ERR_STATE;
            pthread_mutex_unlock(&st->chain_lock);
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
        const eqf_chain_stats *s = st->chain ? eqf_chain_snapshot(st->chain) : NULL;
        snprintf(reply, reply_len,
                 "{\"ok\":true,\"version\":\"%s\",\"channels\":%u,"
                 "\"sample_rate\":%u,\"buffer_size\":%u,"
                 "\"latency_frames\":%u,\"bypass\":%s,"
                 "\"momentary_lufs\":%.2f,\"out_peak_db\":%.2f,"
                 "\"limiter_gain_db\":%.2f}",
                 eqf_version_string(), st->channels,
                 jack_get_sample_rate(st->client),
                 jack_get_buffer_size(st->client),
                 st->chain ? eqf_chain_latency(st->chain) : 0,
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
    pthread_mutex_init(&st->chain_lock, NULL);
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

    st->chain = eqf_chain_new(st->channels, jack_get_sample_rate(st->client),
                              RT_MAX_BLOCK);
    if (!st->chain) {
        fprintf(stderr, "eqforge-rt: chain allocation failed\n");
        return 2;
    }
    if (config) {
        int rc = eqf_chain_configure_file(st->chain, config);
        if (rc != EQF_OK)
            fprintf(stderr, "eqforge-rt: initial config rejected: %s\n",
                    eqf_result_str(rc));
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
    eqf_chain_free(st->chain);
    return 0;
}
