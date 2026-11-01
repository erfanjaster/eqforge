/* EQForge native core - self-contained test suite.
 *
 * Verifies:
 *  - JSON parser correctness (values, escapes, errors)
 *  - biquad designs (gain at fc, attenuation far from fc, unity for bypass)
 *  - EQ bank loading and magnitude response
 *  - compressor gain reduction behavior
 *  - limiter keeps output under ceiling, detects true peaks
 *  - crossfeed mixes channels
 *  - convolver applies an IR (delta = passthrough)
 *  - meter LUFS of a known 1 kHz sine (~ -23.0 LUFS at -20 dBFS RMS)
 *  - chain end-to-end: JSON config, processing, latency, snapshot
 *
 * SPDX-License-Identifier: MIT
 */
#include <assert.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

#include "eqforge/eqforge.h"

static int tests_run = 0;
static int tests_failed = 0;

#define CHECK(cond)                                                        \
    do {                                                                   \
        tests_run++;                                                       \
        if (!(cond)) {                                                     \
            tests_failed++;                                                \
            printf("  FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);       \
        }                                                                  \
    } while (0)

#define CHECK_NEAR(a, b, eps)                                              \
    do {                                                                   \
        tests_run++;                                                       \
        double _a = (double)(a), _b = (double)(b), _e = (double)(eps);     \
        if (fabs(_a - _b) > _e) {                                          \
            tests_failed++;                                                \
            printf("  FAIL %s:%d: %s ~= %s (%g vs %g, eps %g)\n",          \
                   __FILE__, __LINE__, #a, #b, _a, _b, _e);                \
        }                                                                  \
    } while (0)

static double db(double lin) { return 20.0 * log10(fabs(lin) + 1e-12); }

/* ---------------- JSON ---------------- */

static void test_json(void)
{
    printf("test_json\n");
    eqf_json *doc = NULL;
    char err[128];
    int rc = eqf_json_parse("{\"a\": 1.5, \"b\": [true, null, \"x\\u00e9y\"], "
                            "\"c\": {\"d\": -2e3}}", 0, &doc, err, sizeof(err));
    CHECK(rc == EQF_OK);
    CHECK(doc != NULL);
    CHECK_NEAR(eqf_json_obj_number(doc, "a", 0), 1.5, 1e-12);
    const eqf_json *b = eqf_json_obj_array(doc, "b");
    CHECK(eqf_json_arr_size(b) == 3);
    CHECK(eqf_json_get_bool(eqf_json_arr_get(b, 0), false) == true);
    CHECK(eqf_json_arr_get(b, 1)->type == EQF_JSON_NULL);
    CHECK(strcmp(eqf_json_get_string(eqf_json_arr_get(b, 2), ""), "x\xc3\xa9y") == 0);
    const eqf_json *c = eqf_json_obj_get(doc, "c");
    CHECK_NEAR(eqf_json_obj_number(c, "d", 0), -2000.0, 1e-9);
    eqf_json_free(doc);

    doc = NULL;
    rc = eqf_json_parse("{invalid", 0, &doc, err, sizeof(err));
    CHECK(rc == EQF_ERR_PARSE);
    CHECK(doc == NULL);

    doc = NULL;
    rc = eqf_json_parse("[1,2] trailing", 0, &doc, err, sizeof(err));
    CHECK(rc == EQF_ERR_PARSE);

    /* deep nesting guard */
    char deep[4096];
    int n = 0;
    for (int i = 0; i < 100; i++) deep[n++] = '[';
    for (int i = 0; i < 100; i++) deep[n++] = ']';
    deep[n] = '\0';
    doc = NULL;
    rc = eqf_json_parse(deep, 0, &doc, err, sizeof(err));
    CHECK(rc == EQF_ERR_PARSE); /* exceeds MAX_DEPTH */
}

/* ---------------- biquad ---------------- */

static void test_biquad_design(void)
{
    printf("test_biquad_design\n");
    eqf_biquad_coeffs c;
    double sr = 48000.0;

    /* peaking +6 dB at 1 kHz, Q=1 */
    CHECK(eqf_biquad_design(&c, EQF_FILTER_PEAK, 1000.0, 6.0, 1.0, true, sr) == EQF_OK);
    CHECK_NEAR(db(eqf_biquad_magnitude(&c, 1000.0, sr)), 6.0, 0.05);
    CHECK_NEAR(db(eqf_biquad_magnitude(&c, 20.0, sr)), 0.0, 0.05);
    CHECK_NEAR(db(eqf_biquad_magnitude(&c, 20000.0, sr)), 0.0, 0.05);

    /* lowpass: ~-3dB at fc, strong attenuation above */
    CHECK(eqf_biquad_design(&c, EQF_FILTER_LOWPASS, 1000.0, 0.0, 0.7071, true, sr) == EQF_OK);
    CHECK_NEAR(db(eqf_biquad_magnitude(&c, 1000.0, sr)), -3.0, 0.1);
    CHECK(db(eqf_biquad_magnitude(&c, 10000.0, sr)) < -18.0);

    /* highpass */
    CHECK(eqf_biquad_design(&c, EQF_FILTER_HIGHPASS, 100.0, 0.0, 0.7071, true, sr) == EQF_OK);
    CHECK_NEAR(db(eqf_biquad_magnitude(&c, 100.0, sr)), -3.0, 0.1);
    CHECK(db(eqf_biquad_magnitude(&c, 10.0, sr)) < -18.0);

    /* low shelf +6 dB: boosts lows, flat at highs */
    CHECK(eqf_biquad_design(&c, EQF_FILTER_LOW_SHELF, 100.0, 6.0, 0.71, false, sr) == EQF_OK);
    CHECK_NEAR(db(eqf_biquad_magnitude(&c, 20.0, sr)), 6.0, 0.2);
    CHECK_NEAR(db(eqf_biquad_magnitude(&c, 10000.0, sr)), 0.0, 0.2);

    /* high shelf -6 dB */
    CHECK(eqf_biquad_design(&c, EQF_FILTER_HIGH_SHELF, 8000.0, -6.0, 0.71, false, sr) == EQF_OK);
    CHECK_NEAR(db(eqf_biquad_magnitude(&c, 20000.0, sr)), -6.0, 0.25);
    CHECK_NEAR(db(eqf_biquad_magnitude(&c, 100.0, sr)), 0.0, 0.2);

    /* notch kills fc */
    CHECK(eqf_biquad_design(&c, EQF_FILTER_NOTCH, 1000.0, 0.0, 5.0, true, sr) == EQF_OK);
    CHECK(db(eqf_biquad_magnitude(&c, 1000.0, sr)) < -40.0);

    /* clamping: design at nyquist+ must not explode */
    CHECK(eqf_biquad_design(&c, EQF_FILTER_PEAK, 30000.0, 6.0, 1.0, true, sr) == EQF_OK);
}

static void test_biquad_process(void)
{
    printf("test_biquad_process\n");
    /* 1 kHz sine through a +6 dB peak filter at 1 kHz -> amplitude x2 */
    double sr = 48000.0;
    eqf_biquad f;
    eqf_biquad_init(&f);
    eqf_biquad_coeffs c;
    eqf_biquad_design(&c, EQF_FILTER_PEAK, 1000.0, 6.0, 1.0, true, sr);
    eqf_biquad_set_target(&f, &c, 0);

    enum { N = 4800 };
    float buf[N];
    for (int i = 0; i < N; i++)
        buf[i] = (float)sin(2.0 * M_PI * 1000.0 * i / sr);
    /* measure steady-state amplitude in the last 1000 samples */
    eqf_biquad_process(&f, buf, N);
    float peak = 0.0f;
    for (int i = N - 1000; i < N; i++)
        if (fabsf(buf[i]) > peak)
            peak = fabsf(buf[i]);
    CHECK_NEAR(db(peak), 6.0, 0.15); /* input peak = 0 dB */

    /* smoothing: ramped target must converge to the same steady state */
    eqf_biquad f2;
    eqf_biquad_init(&f2);
    eqf_biquad_set_target(&f2, &c, 2400); /* 50 ms ramp */
    for (int i = 0; i < N; i++)
        buf[i] = (float)sin(2.0 * M_PI * 1000.0 * i / sr);
    eqf_biquad_process(&f2, buf, N);
    peak = 0.0f;
    for (int i = N - 1000; i < N; i++)
        if (fabsf(buf[i]) > peak)
            peak = fabsf(buf[i]);
    CHECK_NEAR(db(peak), 6.0, 0.15);
}

/* ---------------- EQ bank ---------------- */

static void test_eq(void)
{
    printf("test_eq\n");
    eqf_eq eq;
    eqf_eq_init(&eq, 2, 48000);

    eqf_band_spec bands[3];
    memset(bands, 0, sizeof(bands));
    bands[0].type = EQF_FILTER_PEAK; bands[0].freq = 1000; bands[0].gain_db = 6;
    bands[0].q = 1.0; bands[0].use_q = true; bands[0].enabled = true; bands[0].channel = -1;
    bands[1].type = EQF_FILTER_HIGHPASS; bands[1].freq = 40; bands[1].q = 0.707;
    bands[1].use_q = true; bands[1].enabled = true; bands[1].channel = -1;
    bands[2].type = EQF_FILTER_PEAK; bands[2].freq = 250; bands[2].gain_db = -3;
    bands[2].q = 2.0; bands[2].use_q = true; bands[2].enabled = true; bands[2].channel = 1;

    CHECK(eqf_eq_load(&eq, bands, 3, 0) == EQF_OK);
    CHECK(eq.bands_per_channel[0] == 2);
    CHECK(eq.bands_per_channel[1] == 3);

    /* response at 1 kHz should include +6 dB peak (HP ~0 dB there) */
    double r0 = eqf_eq_response(&eq, 0, 1000.0);
    CHECK_NEAR(db(r0), 6.0, 0.1);
    /* channel 1 at 250 Hz carries the extra -3 dB band */
    double r1 = eqf_eq_response(&eq, 1, 250.0);
    double r0b = eqf_eq_response(&eq, 0, 250.0);
    CHECK(db(r1) < db(r0b) - 2.0);

    /* process: DC must be killed by the highpass */
    enum { N = 4800 };
    float l[N], r[N];
    float *ptrs[2] = { l, r };
    for (int i = 0; i < N; i++) { l[i] = 1.0f; r[i] = 1.0f; }
    eqf_eq_process(&eq, ptrs, N);
    CHECK(fabsf(l[N - 1]) < 0.05f);

    /* disabled bands are skipped */
    bands[0].enabled = false;
    CHECK(eqf_eq_load(&eq, bands, 3, 0) == EQF_OK);
    CHECK_NEAR(db(eqf_eq_response(&eq, 0, 1000.0)), 0.0, 0.05);
}

/* ---------------- compressor ---------------- */

static void test_compressor(void)
{
    printf("test_compressor\n");
    eqf_compressor cp;
    eqf_compressor_init(&cp, 1, 48000);
    eqf_compressor_config cfg;
    eqf_compressor_config_default(&cfg);
    cfg.threshold_db = -20.0;
    cfg.ratio = 4.0;
    cfg.attack_ms = 1.0;
    cfg.release_ms = 50.0;
    cfg.knee_db = 0.0;
    CHECK(eqf_compressor_configure(&cp, &cfg) == EQF_OK);
    eqf_compressor_set_active(&cp, true);

    /* loud 0 dBFS sine should be pulled toward threshold */
    enum { N = 48000 };
    static float buf[N];
    for (int i = 0; i < N; i++)
        buf[i] = (float)sin(2.0 * M_PI * 1000.0 * i / 48000.0);
    float *ptrs[1] = { buf };
    eqf_compressor_process(&cp, ptrs, N);
    float peak = 0.0f;
    for (int i = N - 4800; i < N; i++)
        if (fabsf(buf[i]) > peak)
            peak = fabsf(buf[i]);
    /* 0 dBFS in, ratio 4: peak should land roughly at -15 dBFS */
    CHECK(db(peak) < -10.0);
    CHECK(db(peak) > -22.0);
    CHECK(eqf_compressor_gain_reduction_db(&cp) > 5.0);

    /* quiet signal passes untouched */
    eqf_compressor_reset(&cp);
    for (int i = 0; i < N; i++)
        buf[i] = 0.01f * (float)sin(2.0 * M_PI * 1000.0 * i / 48000.0);
    eqf_compressor_process(&cp, ptrs, N);
    peak = 0.0f;
    for (int i = N - 4800; i < N; i++)
        if (fabsf(buf[i]) > peak)
            peak = fabsf(buf[i]);
    CHECK_NEAR(db(peak), db(0.01), 0.5);
}

/* ---------------- limiter ---------------- */

static void test_limiter(void)
{
    printf("test_limiter\n");
    eqf_limiter lim;
    eqf_limiter_init(&lim, 2, 48000);
    eqf_limiter_config cfg;
    eqf_limiter_config_default(&cfg);
    cfg.ceiling_db = -3.0;
    cfg.attack_ms = 0.5;
    cfg.release_ms = 40.0;
    cfg.lookahead_ms = 2.0;
    CHECK(eqf_limiter_configure(&lim, &cfg) == EQF_OK);
    eqf_limiter_set_active(&lim, true);
    CHECK(eqf_limiter_latency(&lim) == 96);

    enum { N = 48000 };
    static float l[N], r[N];
    for (int i = 0; i < N; i++) {
        float v = (float)(1.8 * sin(2.0 * M_PI * 1000.0 * i / 48000.0));
        l[i] = v;
        r[i] = v;
    }
    float *ptrs[2] = { l, r };
    eqf_limiter_process(&lim, ptrs, N);

    float peak = 0.0f;
    for (int i = N - 24000; i < N; i++) {
        if (fabsf(l[i]) > peak) peak = fabsf(l[i]);
        if (fabsf(r[i]) > peak) peak = fabsf(r[i]);
    }
    float ceil_lin = (float)pow(10.0, -3.0 / 20.0);
    CHECK(peak <= ceil_lin + 1e-5f);
    /* steady-state gain should approach ceiling/input = -3dB - (+5.1dB) */
    CHECK(peak > 0.3f * ceil_lin); /* not squashed to nothing */

    /* true-peak accuracy: a slow 100 Hz sine at amplitude 0.8 has a true
     * peak of 0.8 regardless of sample alignment; the 4x interpolating
     * detector must estimate it within +/-2%. */
    eqf_limiter_reset(&lim);
    for (int i = 0; i < 4800; i++) {
        float v = 0.8f * (float)sin(2.0 * M_PI * 100.0 * i / 48000.0 + 0.7);
        l[i] = v; r[i] = v;
    }
    eqf_limiter_process(&lim, ptrs, 4800);
    CHECK_NEAR(eqf_limiter_true_peak(&lim), 0.8, 0.02);
}

/* ---------------- crossfeed ---------------- */

static void test_crossfeed(void)
{
    printf("test_crossfeed\n");
    eqf_crossfeed xf;
    eqf_crossfeed_init(&xf, 48000);
    eqf_crossfeed_config cfg;
    eqf_crossfeed_config_default(&cfg);
    cfg.enabled = true;
    cfg.level_db = -6.0;
    cfg.fc_hz = 700.0;
    CHECK(eqf_crossfeed_configure(&xf, &cfg) == EQF_OK);
    eqf_crossfeed_set_active(&xf, true);

    enum { N = 48000 };
    static float l[N], r[N];
    /* DC-free low tone in L only */
    for (int i = 0; i < N; i++) {
        l[i] = (float)sin(2.0 * M_PI * 100.0 * i / 48000.0);
        r[i] = 0.0f;
    }
    eqf_crossfeed_process(&xf, l, r, N);
    /* R should now carry an attenuated copy */
    float rp = 0.0f;
    for (int i = N - 4800; i < N; i++)
        if (fabsf(r[i]) > rp)
            rp = fabsf(r[i]);
    CHECK(rp > 0.2f);   /* ~ -6 dB of 1.0 through two LP stages ~ 0.5 */
    CHECK(rp < 0.9f);

    /* high frequencies should NOT cross much (LP @ 700 Hz) */
    for (int i = 0; i < N; i++) {
        l[i] = (float)sin(2.0 * M_PI * 10000.0 * i / 48000.0);
        r[i] = 0.0f;
    }
    eqf_crossfeed_reset(&xf);
    eqf_crossfeed_process(&xf, l, r, N);
    rp = 0.0f;
    for (int i = N - 4800; i < N; i++)
        if (fabsf(r[i]) > rp)
            rp = fabsf(r[i]);
    CHECK(rp < 0.15f);
}

/* ---------------- convolver ---------------- */

static void test_convolver(void)
{
    printf("test_convolver\n");
    eqf_convolver cv;
    eqf_convolver_init(&cv, 1);

    /* delta IR = passthrough */
    float ir[8] = { 1, 0, 0, 0, 0, 0, 0, 0 };
    CHECK(eqf_convolver_set_ir(&cv, 0, ir, 8) == EQF_OK);
    eqf_convolver_set_active(&cv, true);

    enum { N = 1024 };
    float buf[N];
    for (int i = 0; i < N; i++)
        buf[i] = (float)sin(2.0 * M_PI * 440.0 * i / 48000.0);
    float expected[N];
    memcpy(expected, buf, sizeof(buf));
    float *ptrs[1] = { buf };
    eqf_convolver_process(&cv, ptrs, N);
    double err = 0.0;
    for (int i = 0; i < N; i++)
        err += fabs(buf[i] - expected[i]);
    CHECK(err < 1e-3);

    /* 2-tap IR [0.5 0.5] = averaging filter: DC preserved, fs/2 killed */
    float ir2[2] = { 0.5f, 0.5f };
    CHECK(eqf_convolver_set_ir(&cv, 0, ir2, 2) == EQF_OK);
    eqf_convolver_reset(&cv);
    for (int i = 0; i < N; i++)
        buf[i] = (i % 2 == 0) ? 1.0f : -1.0f; /* fs/2 */
    eqf_convolver_process(&cv, ptrs, N);
    CHECK(fabsf(buf[N - 1]) < 1e-5f);
}

/* ---------------- meter ---------------- */

static void test_meter(void)
{
    printf("test_meter\n");
    eqf_meter m;
    CHECK(eqf_meter_init(&m, 1, 48000) == EQF_OK);

    /* Anchor: 1 kHz sine at -20 dBFS PEAK (RMS = -23.01 dBFS) must measure
     * -23.0 LUFS (ITU-R BS.1770 conformance: K-weight gain at 1 kHz is
     * +0.70 dB, and the loudness formula offset is -0.691). */
    double sr = 48000.0;
    double amp = 0.1; /* -20 dBFS peak */
    enum { BLK = 480 };
    float buf[BLK];
    float *ptrs[1] = { buf };
    int total = 48000 * 2; /* 2 s */
    for (int i = 0; i < total; i += BLK) {
        for (int j = 0; j < BLK; j++)
            buf[j] = (float)(amp * sin(2.0 * M_PI * 1000.0 * (i + j) / sr));
        eqf_meter_update(&m, (const float *const *)ptrs, BLK);
    }
    CHECK_NEAR(m.momentary_lufs, -23.00, 0.15);
    CHECK_NEAR(db(m.peak[0]), -20.0, 0.2);
    CHECK(m.clipped == false);

    /* clipping flag */
    for (int j = 0; j < BLK; j++)
        buf[j] = 1.2f;
    eqf_meter_update(&m, (const float *const *)ptrs, BLK);
    CHECK(m.clipped == true);

    /* true peak accuracy: 100 Hz sine amplitude 0.9 -> true peak ~0.9 */
    eqf_meter_reset(&m);
    for (int i = 0; i < 4800; i += BLK) {
        for (int j = 0; j < BLK; j++)
            buf[j] = 0.9f * (float)sin(2.0 * M_PI * 100.0 * (i + j) / sr + 0.7);
        eqf_meter_update(&m, (const float *const *)ptrs, BLK);
    }
    CHECK_NEAR(m.true_peak[0], 0.9, 0.02);
    eqf_meter_free(&m);
}

/* ---------------- chain ---------------- */

static void test_chain(void)
{
    printf("test_chain\n");
    eqf_chain *c = eqf_chain_new(2, 48000, 1024);
    CHECK(c != NULL);
    CHECK(eqf_chain_latency(c) == 0);

    const char *cfg =
        "{"
        "  \"preamp_db\": -6.0,"
        "  \"eq\": { \"filters\": ["
        "     {\"type\": \"peak\", \"freq\": 1000, \"gain_db\": 12, \"q\": 1.0},"
        "     {\"type\": \"highpass\", \"freq\": 30, \"q\": 0.707}"
        "  ]},"
        "  \"limiter\": {\"enabled\": true, \"ceiling_db\": -1.0,"
        "               \"attack_ms\": 5, \"release_ms\": 50, \"lookahead_ms\": 2}"
        "}";
    CHECK(eqf_chain_configure_json(c, cfg, 0) == EQF_OK);
    CHECK(eqf_chain_latency(c) == 96);

    /* feed a loud 1 kHz sine; preamp -6 then +12 peak => net +6 dB => limiter
     * must hold output at or below -1 dBTP */
    enum { BLK = 256 };
    static float l[BLK], r[BLK];
    float *in[2] = { l, r };
    float *out[2] = { l, r };
    double sr = 48000.0;
    for (int blk = 0; blk < 48000 / BLK * 2; blk++) {
        for (int i = 0; i < BLK; i++) {
            double t = (blk * BLK + i) / sr;
            float v = (float)(0.9 * sin(2.0 * M_PI * 1000.0 * t));
            l[i] = v; r[i] = v;
        }
        CHECK(eqf_chain_process(c, in, out, BLK) == EQF_OK);
    }
    const eqf_chain_stats *s = eqf_chain_snapshot(c);
    CHECK(s != NULL);
    CHECK(s->out_peak[0] <= pow(10.0, -1.0 / 20.0) + 1e-5);
    CHECK(s->momentary_lufs > -30.0f && s->momentary_lufs < 0.0f);
    CHECK(s->limiter_gain_db < -1.0f); /* limiter engaged */

    /* bypass passes audio ~ unprocessed (aside from fade) */
    eqf_chain_set_bypass(c, true);
    for (int blk = 0; blk < 4; blk++) {
        for (int i = 0; i < BLK; i++) { l[i] = 0.5f; r[i] = 0.5f; }
        eqf_chain_process(c, in, out, BLK);
    }
    for (int i = 0; i < BLK; i++) {
        CHECK(fabsf(l[i] - 0.5f) < 1e-5f);
        CHECK(fabsf(r[i] - 0.5f) < 1e-5f);
    }
    eqf_chain_set_bypass(c, false);

    /* invalid JSON rejected, chain keeps running */
    CHECK(eqf_chain_configure_json(c, "{oops", 0) == EQF_ERR_PARSE);
    CHECK(eqf_chain_last_error(c) != NULL);
    for (int i = 0; i < BLK; i++) { l[i] = 0.1f; r[i] = 0.1f; }
    CHECK(eqf_chain_process(c, in, out, BLK) == EQF_OK);

    /* bad parameter ranges rejected */
    CHECK(eqf_chain_configure_json(c, "{\"preamp_db\": 200}", 0) == EQF_ERR_PARSE);
    CHECK(eqf_chain_configure_json(c,
        "{\"eq\":{\"filters\":[{\"type\":\"wat\",\"freq\":100}]}}", 0) == EQF_ERR_PARSE);

    /* sample-rate change with reload */
    CHECK(eqf_chain_set_sample_rate(c, 44100) == EQF_OK);
    CHECK(eqf_chain_configure_json(c, cfg, 0) == EQF_OK);

    eqf_chain_free(c);
}

static void test_version(void)
{
    printf("test_version\n");
    CHECK(strcmp(eqf_version_string(), "1.0.0") == 0);
    CHECK(eqf_abi_version() == EQF_ABI_VERSION);
}

int main(void)
{
    printf("libeqforge core tests (version %s, ABI %u)\n",
           eqf_version_string(), eqf_abi_version());
    test_version();
    test_json();
    test_biquad_design();
    test_biquad_process();
    test_eq();
    test_compressor();
    test_limiter();
    test_crossfeed();
    test_convolver();
    test_meter();
    test_chain();
    printf("\n%d checks, %d failures\n", tests_run, tests_failed);
    return tests_failed ? 1 : 0;
}
