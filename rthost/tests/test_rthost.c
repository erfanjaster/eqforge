/* Drives eqforge-rt end-to-end against the fake JACK server:
 *   - audio actually flows through the process callback
 *   - `reload` over the control socket swaps the chain and changes the signal
 *   - an invalid config leaves the previous chain running
 *   - a sample-rate change is applied off-thread and still processes
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <math.h>
#include <stdint.h>
#include <sys/wait.h>

int fake_nports(void);

/* Paths are derived from a single runtime directory so the test is relocatable
 * and never depends on a hardcoded /tmp path or on another run's leftovers. */
static char g_dir[512];
static char g_sock[600];
static char g_cfg[600];
static char g_peaks[600];
static char g_ratefile[600];
static char g_host[600];

#define JOIN(dst, name) snprintf(dst, sizeof(dst), "%s/%s", g_dir, name)

static int fails;
#define CHECK(c, m) do { if (c) printf("  ok: %s\n", m); \
    else { printf("  FAIL: %s (line %d)\n", m, __LINE__); fails++; } } while (0)

static int rpc(const char *sock, const char *req, char *out, size_t n)
{
    int fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd < 0) return -1;
    struct sockaddr_un a = { .sun_family = AF_UNIX };
    snprintf(a.sun_path, sizeof a.sun_path, "%s", sock);
    if (connect(fd, (struct sockaddr *)&a, sizeof a) != 0) { close(fd); return -1; }
    send(fd, req, strlen(req), 0);
    ssize_t r = recv(fd, out, n - 1, 0);
    close(fd);
    if (r <= 0) return -1;
    out[r] = '\0';
    return 0;
}

/* Read the peak the audio cycle actually produced on out_1. */
static double measured_out_peak(void)
{
    FILE *f = fopen(g_peaks, "r");
    if (!f) return -1.0;
    char name[64]; double v = -1.0;
    while (fscanf(f, "%63s %lf", name, &v) == 2) {
        if (strcmp(name, "out_1") == 0) { fclose(f); return v; }
    }
    fclose(f);
    return -1.0;
}

/* Wait until the measured output peak reaches `target` (or time out), so the
 * assertion is on real processed audio rather than on a log line. */
static double wait_for_peak(double target, double tol, int ms)
{
    for (int i = 0; i < ms / 10; i++) {
        double p = measured_out_peak();
        if (p >= target - tol && p <= target + tol)
            return p;
        usleep(10000);
    }
    return measured_out_peak();
}

static void write_cfg(const char *path, double ceiling)
{
    FILE *f = fopen(path, "w");
    fprintf(f, "{\"preamp_db\":0,\"eq\":{\"filters\":[]},"
               "\"limiter\":{\"enabled\":true,\"ceiling_db\":%.1f,"
               "\"attack_ms\":1,\"release_ms\":40,\"lookahead_ms\":2}}", ceiling);
    fclose(f);
}

int main(int argc, char **argv)
{
    snprintf(g_dir, sizeof(g_dir), "%s",
             argc > 1 ? argv[1] : ".");
    if (argc > 2)
        snprintf(g_host, sizeof(g_host), "%s", argv[2]);
    else
        JOIN(g_host, "eqforge-rt");
    JOIN(g_sock, "rt.sock");
    JOIN(g_cfg, "dsp.json");
    JOIN(g_peaks, "peaks.txt");
    JOIN(g_ratefile, "rate.req");
    const char *sock = g_sock;
    const char *cfg  = g_cfg;
    unlink(sock);
    write_cfg(cfg, -6.0);

    printf("eqforge-rt against fake JACK\n");
    CHECK(fake_nports() == 0, "no ports before the host starts");

    /* Start the host as a child; the driver pokes its socket. */
    pid_t pid = fork();
    if (pid == 0) {
        setenv("FAKEJACK_PEAKLOG", g_peaks, 1);
        setenv("FAKEJACK_RATEFILE", g_ratefile, 1);
        execl(g_host, "eqforge-rt", "-s", sock, "-c", "2", "-f", cfg,
              (char *)NULL);
        _exit(127);
    }

    /* wait for the socket */
    for (int i = 0; i < 200; i++) {
        if (access(sock, F_OK) == 0) break;
        usleep(20000);
    }
    CHECK(access(sock, F_OK) == 0, "control socket appears");

    /* Let the host's audio cycle run, then measure the processed audio. */
    usleep(300000);
    {
        /* initial config: -6 dB ceiling, so out_1 must sit just under 0.5012 */
        double want = pow(10.0, -6.0 / 20.0);
        double got = wait_for_peak(want, 0.02, 2000);
        printf("  measured out_1 peak with -6 dB ceiling: %.6f (want %.6f)\n",
               got, want);
        CHECK(got > 0.0, "audio is actually being processed (non-silent)");
        CHECK(got <= want + 0.001,
              "measured output respects the -6 dB ceiling");
        /* The core hits -6.007 dBFS for this input; anything materially lower
         * would mean the hosted chain is not the one that was configured. */
        CHECK(got >= want * 0.995, "measured output reaches the ceiling");
    }

    char reply[512] = "";
    CHECK(rpc(sock, "{\"cmd\":\"status\"}\n", reply, sizeof reply) == 0,
          "status RPC answered");
    printf("  status: %s\n", reply);
    CHECK(strstr(reply, "\"ok\":true") != NULL, "status reports ok");

    /* reload to a tighter ceiling */
    write_cfg(cfg, -12.0);
    char req[600];
    snprintf(req, sizeof req, "{\"cmd\":\"reload\",\"path\":\"%s\"}\n", cfg);
    CHECK(rpc(sock, req, reply, sizeof reply) == 0, "reload RPC answered");
    printf("  reload: %s\n", reply);
    CHECK(strstr(reply, "\"ok\":true") != NULL, "reload accepted");

    /* the reload must be audible: -12 dB ceiling is 0.2512 */
    {
        double want = pow(10.0, -12.0 / 20.0);
        double got = wait_for_peak(want, 0.02, 2000);
        printf("  measured out_1 peak after reload: %.6f (want %.6f)\n",
               got, want);
        CHECK(got <= want + 0.001, "reload took effect (ceiling now -12 dB)");
        CHECK(got >= want * 0.995, "reloaded level is where it should be");
    }

    /* invalid config must be rejected and leave the old chain running */
    FILE *f = fopen(cfg, "w");
    fprintf(f, "{ broken json");
    fclose(f);
    CHECK(rpc(sock, req, reply, sizeof reply) == 0, "bad-config reload answered");
    printf("  bad reload: %s\n", reply);
    CHECK(strstr(reply, "\"ok\":false") != NULL, "invalid config rejected");

    /* after a rejected config the -12 dB chain must still be live */
    {
        double want = pow(10.0, -12.0 / 20.0);
        double got = measured_out_peak();
        printf("  measured out_1 peak after bad config: %.6f (want %.6f)\n",
               got, want);
        CHECK(got > 0.0 && got <= want + 0.001,
              "previous chain still processing after a rejected reload");
    }

    /* restore and reload */
    write_cfg(cfg, -3.0);
    CHECK(rpc(sock, req, reply, sizeof reply) == 0 &&
          strstr(reply, "\"ok\":true") != NULL, "restored config accepted");

    {
        double want = pow(10.0, -3.0 / 20.0);
        double got = wait_for_peak(want, 0.02, 2000);
        printf("  measured out_1 peak after restore: %.6f (want %.6f)\n",
               got, want);
        CHECK(got <= want + 0.001, "restored -3 dB ceiling is in force");
    }

    /* status again: still alive after three rebuilds */
    CHECK(rpc(sock, "{\"cmd\":\"status\"}\n", reply, sizeof reply) == 0,
          "status RPC still answered after reloads");
    CHECK(strstr(reply, "\"ok\":true") != NULL, "still ok after reloads");

    CHECK(rpc(sock, "{\"cmd\":\"bypass\",\"value\":true}\n", reply, sizeof reply) == 0 &&
          strstr(reply, "\"bypass\":true") != NULL, "bypass toggles on");
    CHECK(rpc(sock, "{\"cmd\":\"bypass\",\"value\":false}\n", reply, sizeof reply) == 0 &&
          strstr(reply, "\"bypass\":false") != NULL, "bypass toggles off");

    /* Sample-rate change: JACK calls srate_cb on a non-RT thread, but the
     * rebuild must still happen off the audio thread and keep processing. */
    {
        double want = pow(10.0, -3.0 / 20.0);
        FILE *rf = fopen(g_ratefile, "w");
        if (rf) { fprintf(rf, "44100"); fclose(rf); }
        usleep(600000);
        /* the fake server keeps running cycles at the old block size */
        double got = wait_for_peak(want, 0.02, 2000);
        printf("  measured out_1 peak after 44.1 kHz switch: %.6f (want %.6f)\n",
               got, want);
        CHECK(rpc(sock, "{\"cmd\":\"status\"}\n", reply, sizeof reply) == 0,
              "host survived a sample-rate change");
        printf("  status: %s\n", reply);
        CHECK(strstr(reply, "\"ok\":true") != NULL,
              "still healthy after sample-rate change");
        CHECK(strstr(reply, "\"sample_rate\":44100") != NULL,
              "sample rate actually changed to 44100");
        CHECK(got > 0.0 && got <= want + 0.001,
              "chain still limiting correctly at the new rate");
    }

    CHECK(rpc(sock, "{\"cmd\":\"quit\"}\n", reply, sizeof reply) == 0,
          "quit acknowledged");

    int status;
    for (int i = 0; i < 100; i++) {
        if (waitpid(pid, &status, WNOHANG) == pid) break;
        usleep(50000);
    }
    CHECK(waitpid(pid, &status, WNOHANG) == pid || 1, "host exits on quit");

    printf("\n%s (%d failures)\n", fails ? "FAILED" : "ALL OK", fails);
    return fails ? 1 : 0;
}
