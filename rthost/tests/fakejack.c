/* Functional fake libjack: enough of the API to run eqforge-rt's control
 * socket, reload path and process callback in-process, without a sound server.
 * Compiling against a stub proves syntax; running against this proves the
 * reload/retire logic actually works. */
#define _GNU_SOURCE
#include <jack/jack.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <pthread.h>
#include <stdatomic.h>
#include <time.h>
#include <unistd.h>
#include <math.h>

#define FAKE_MAX_PORTS 32
static _Atomic jack_nframes_t g_rate = 48000;
static jack_nframes_t g_bsize = 256;
static int (*g_process)(jack_nframes_t, void *);
static void *g_process_arg;
static void (*g_latency)(jack_latency_callback_mode_t, void *);
static void *g_latency_arg;
static int (*g_srate)(jack_nframes_t, void *);
static void *g_srate_arg;
static int (*g_bsize_cb)(jack_nframes_t, void *);
static void *g_bsize_arg;
static void (*g_shutdown)(void *);
static void *g_shutdown_arg;
struct port { char name[64]; float *buf; int is_input; };
static struct port g_ports[FAKE_MAX_PORTS];
static int g_nports;

jack_client_t *jack_client_open(const char *n, jack_options_t o, jack_status_t *s)
{ (void)n;(void)o; if(s)*s=0; return (jack_client_t*)1; }
int jack_client_close(jack_client_t *c){ (void)c; return 0; }
void jack_on_shutdown(jack_client_t *c, void (*fn)(void*), void *a)
{ (void)c; g_shutdown=fn; g_shutdown_arg=a; }
int jack_set_process_callback(jack_client_t*c,int(*fn)(jack_nframes_t,void*),void*a)
{ (void)c; g_process=fn; g_process_arg=a; return 0; }
int jack_set_latency_callback(jack_client_t*c,void(*fn)(jack_latency_callback_mode_t,void*),void*a)
{ (void)c; g_latency=fn; g_latency_arg=a; return 0; }
int jack_set_sample_rate_callback(jack_client_t*c,int(*fn)(jack_nframes_t,void*),void*a)
{ (void)c; g_srate=fn; g_srate_arg=a; return 0; }
int jack_set_buffer_size_callback(jack_client_t*c,int(*fn)(jack_nframes_t,void*),void*a)
{ (void)c; g_bsize_cb=fn; g_bsize_arg=a; return 0; }
jack_port_t *jack_port_register(jack_client_t*c,const char*n,const char*t,
                                unsigned long f,unsigned long b)
{ (void)c;(void)t;(void)f;(void)b;
  if (g_nports>=FAKE_MAX_PORTS) return NULL;
  snprintf(g_ports[g_nports].name,sizeof g_ports[g_nports].name,"%s",n);
  g_ports[g_nports].is_input = (f & JackPortIsInput) ? 1 : 0;
  g_ports[g_nports].buf=calloc(8192,sizeof(float));
  return (jack_port_t*)&g_ports[g_nports++]; }
void *jack_port_get_buffer(jack_port_t *p, jack_nframes_t n)
{ (void)n; return ((struct port*)p)->buf; }
const char *jack_port_name(const jack_port_t *p){ return ((struct port*)p)->name; }
void jack_port_set_latency_range(jack_port_t *p, jack_latency_callback_mode_t m,
                                 jack_latency_range_t *r)
{ (void)p;(void)m;(void)r; }
jack_nframes_t jack_get_sample_rate(jack_client_t *c){(void)c;return g_rate;}
jack_nframes_t jack_get_buffer_size(jack_client_t *c){(void)c;return g_bsize;}

/* ---- audio cycle thread: reproduces what a real JACK server does ---- */
static _Atomic int g_running;
static pthread_t g_cycle;

static void *cycle_thread(void *arg)
{
    (void)arg;
    /* One phase per input port. A single shared phase advanced inside the
     * per-port loop leaves 256-sample gaps in each channel's waveform, i.e.
     * a discontinuous input that a true-peak limiter legitimately clamps
     * harder. Each port now gets its own continuous sine. */
    static double phase[FAKE_MAX_PORTS];
    const double w = 2.0 * 3.14159265358979 * 1000.0 / 48000.0;
    while (atomic_load(&g_running)) {
        /* Cross-process control: the test writes a rate into FAKEJACK_RATEFILE
         * and this (the host's own) cycle thread applies it, calling the
         * registered callback exactly like a real server would. */
        const char *rf = getenv("FAKEJACK_RATEFILE");
        if (rf) {
            FILE *f = fopen(rf, "r");
            if (f) {
                unsigned r = 0;
                jack_nframes_t cur = atomic_load(&g_rate);
                if (fscanf(f, "%u", &r) == 1 && r > 0 && r != cur) {
                    atomic_store(&g_rate, r);
                    if (g_srate) g_srate(r, g_srate_arg);
                }
                fclose(f);
                unlink(rf);
            }
        }
        for (int i = 0; i < g_nports; i++) {
            if (!g_ports[i].is_input)
                continue;
            for (uint32_t j = 0; j < g_bsize && j < 8192; j++) {
                g_ports[i].buf[j] = (float)(0.9 * sin(phase[i]));
                phase[i] += w;
                if (phase[i] > 2.0 * 3.14159265358979)
                    phase[i] -= 2.0 * 3.14159265358979;
            }
        }
        if (g_process)
            g_process(g_bsize, g_process_arg);

        /* Publish the measured output peak of each output port, like a real
         * server's level meters would. This is what the test asserts on. */
        const char *logpath = getenv("FAKEJACK_PEAKLOG");
        if (logpath) {
            /* Rolling-window maximum per output port.
             * - A single block's peak is not the signal peak: for a 1 kHz sine
             *   in a 256-frame block no sample need land on the crest, which
             *   reads up to ~0.9 dB low.
             * - A session maximum never falls, so it cannot show a level that
             *   a reload has *lowered*.
             * A window of ~256 cycles is long enough to contain a crest and
             * short enough to follow a reload within a fraction of a second. */
            #define PEAK_WINDOW 256
            static double win[FAKE_MAX_PORTS][PEAK_WINDOW];
            static uint32_t win_pos;
            for (int i = 0; i < g_nports; i++) {
                if (g_ports[i].is_input)
                    continue;
                double p = 0;
                for (uint32_t j = 0; j < g_bsize && j < 8192; j++) {
                    float v = g_ports[i].buf[j];
                    if (v < 0) v = -v;
                    if ((double)v > p) p = v;
                }
                win[i][win_pos] = p;
            }
            win_pos = (win_pos + 1) % PEAK_WINDOW;
            FILE *lf = fopen(logpath, "w");
            if (lf) {
                for (int i = 0; i < g_nports; i++) {
                    if (g_ports[i].is_input)
                        continue;
                    double mx = 0;
                    for (int k = 0; k < PEAK_WINDOW; k++)
                        if (win[i][k] > mx) mx = win[i][k];
                    fprintf(lf, "%s %.6f\n", g_ports[i].name, mx);
                }
                fclose(lf);
            }
        }
        struct timespec ts = { 0, 2000000L };   /* 2 ms */
        nanosleep(&ts, NULL);
    }
    return NULL;
}

int jack_activate(jack_client_t *c)
{
    (void)c;
    if (atomic_load(&g_running)) return 0;
    atomic_store(&g_running, 1);
    return pthread_create(&g_cycle, NULL, cycle_thread, NULL);
}
int jack_deactivate(jack_client_t *c)
{
    (void)c;
    if (atomic_exchange(&g_running, 0)) pthread_join(g_cycle, NULL);
    return 0;
}
int jack_connect(jack_client_t*c,const char*a,const char*b){(void)c;(void)a;(void)b;return 0;}
int jack_port_disconnect(jack_client_t*c,jack_port_t*p){(void)c;(void)p;return 0;}
int jack_port_unregister(jack_client_t*c,jack_port_t*p){(void)c;(void)p;return 0;}
void jack_free(void *p){(void)p;}
const char **jack_get_ports(jack_client_t*c,const char*a,const char*b,unsigned long f)
{ (void)c;(void)a;(void)b;(void)f; return NULL; }

/* --- test hooks (not part of the JACK API) --- */
void fake_run_cycle(uint32_t nframes, float amp)
{
    for (int i = 0; i < g_nports; i++)
        for (uint32_t j = 0; j < nframes && j < 8192; j++)
            g_ports[i].buf[j] = amp;
    if (g_process) g_process(nframes, g_process_arg);
}
void fake_latency(void) { if (g_latency) g_latency(JackPlaybackLatency, g_latency_arg); }
void fake_srate_change(uint32_t r)
{ g_rate = r; if (g_srate) g_srate(r, NULL); }
float fake_port_peak(int idx, uint32_t nframes)
{ float p=0; for(uint32_t j=0;j<nframes && j<8192;j++){float v=g_ports[idx].buf[j]; if(v<0)v=-v; if(v>p)p=v;} return p; }
int fake_nports(void){ return g_nports; }
float fake_port_peak_named(const char *name, uint32_t nframes)
{
    for (int i = 0; i < g_nports; i++) {
        if (strcmp(g_ports[i].name, name) != 0)
            continue;
        float p = 0;
        for (uint32_t j = 0; j < nframes && j < 8192; j++) {
            float v = g_ports[i].buf[j];
            if (v < 0) v = -v;
            if (v > p) p = v;
        }
        return p;
    }
    return -1.0f;
}
uint32_t fake_buffer_size(void){ return g_bsize; }
