/* EQForge native core - processing chain implementation.
 *
 * Consumes the resolved "dsp config" JSON produced by the EQForge control
 * plane (or by hand):
 *
 * {
 *   "preamp_db": -3.2,
 *   "eq": { "filters": [ {"type":"peak","freq":105,"gain_db":5.1,"q":1.1,
 *                          "enabled":true,"channel":"all"} ] },
 *   "crossfeed": {"enabled":true,"level_db":-6.0,"fc_hz":700},
 *   "convolver": {"enabled":false,"gain_db":0,"ir":[[..],[..]]},
 *   "compressor": {"enabled":false, ...},
 *   "limiter": {"enabled":true,"ceiling_db":-1.0,"attack_ms":5,
 *                "release_ms":60,"lookahead_ms":1.5,"knee_db":3},
 *   "post_gain_db": 0.0
 * }
 *
 * SPDX-License-Identifier: MIT
 */
#include "eqforge/chain.h"
#include "eqforge/eqforge.h"

#include "eqforge/atomics.h"
#include "eqforge/json.h"

#include <math.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define FADE_SAMPLES_AT(rate, ms) ((uint32_t)((rate) * (ms) / 1000.0) + 1)

struct eqf_chain {
    uint32_t channels;
    uint32_t sample_rate;
    uint32_t max_block;

    eqf_eq eq;
    eqf_crossfeed crossfeed;
    eqf_convolver convolver;
    eqf_compressor compressor;
    eqf_limiter limiter;
    eqf_meter meter_in;
    eqf_meter meter_out;

    float preamp;      /* linear */
    float postgain;    /* linear */
    bool bypass;

    /* scratch (planar pointers + per-channel buffers) */
    float *scratch[EQF_MAX_CHANNELS];
    float *ptrs_in[EQF_MAX_CHANNELS];
    float *ptrs_out[EQF_MAX_CHANNELS];

    /* fade state for click-free config changes */
    uint32_t fade_len;
    uint32_t fade_pos;
    bool fading_in;
    bool fade_enabled;

    /* Lock-free, torn-read-free publication of stats to other threads.
     * A plain struct written by the audio thread and memcpy'd by a control
     * thread races (and can return a mix of old and new fields). The seqlock
     * costs the audio thread two relaxed stores and never blocks it. */
    eqf_chain_stats snap;
    /* C11 atoms via <eqforge/atomics.h> so the same source also compiles as
     * C++ (the C++ canary build caught a plain _Atomic here). */
    EQF_ATOMIC(unsigned) snap_seq;
    char *config_copy;   /* current serialized config */
    char error[192];
};

static void set_err(eqf_chain *c, const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(c->error, sizeof(c->error), fmt, ap);
    va_end(ap);
}

const char *eqf_chain_last_error(const eqf_chain *c)
{
    return c && c->error[0] ? c->error : NULL;
}

eqf_chain *eqf_chain_new(uint32_t channels, uint32_t sample_rate, uint32_t max_block)
{
    if (channels == 0 || channels > EQF_MAX_CHANNELS)
        return NULL;
    if (max_block == 0 || max_block > EQF_MAX_BLOCK)
        max_block = EQF_MAX_BLOCK;

    eqf_chain *c = (eqf_chain *)calloc(1, sizeof(eqf_chain));
    if (!c)
        return NULL;
    c->channels = channels;
    c->sample_rate = sample_rate ? sample_rate : 48000;
    c->max_block = max_block;
    c->preamp = 1.0f;
    c->postgain = 1.0f;
    c->bypass = false;

    eqf_eq_init(&c->eq, channels, c->sample_rate);
    eqf_crossfeed_init(&c->crossfeed, c->sample_rate);
    eqf_convolver_init(&c->convolver, channels);
    eqf_compressor_init(&c->compressor, channels, c->sample_rate);
    eqf_limiter_init(&c->limiter, channels, c->sample_rate);
    if (eqf_meter_init(&c->meter_in, channels, c->sample_rate) != EQF_OK ||
        eqf_meter_init(&c->meter_out, channels, c->sample_rate) != EQF_OK) {
        eqf_chain_free(c);
        return NULL;
    }
    for (uint32_t ch = 0; ch < channels; ch++) {
        c->scratch[ch] = (float *)calloc(max_block, sizeof(float));
        if (!c->scratch[ch]) {
            eqf_chain_free(c);
            return NULL;
        }
    }
    c->fade_len = FADE_SAMPLES_AT(c->sample_rate, 10);
    c->fade_enabled = true;
    c->snap.channels = channels;
    return c;
}

void eqf_chain_free(eqf_chain *c)
{
    if (!c)
        return;
    for (uint32_t ch = 0; ch < EQF_MAX_CHANNELS; ch++)
        free(c->scratch[ch]);
    eqf_meter_free(&c->meter_in);
    eqf_meter_free(&c->meter_out);
    free(c->config_copy);
    free(c);
}

/* ---------- config parsing ---------- */

static enum eqf_filter_type parse_filter_type(const char *s, bool *ok)
{
    *ok = true;
    if (!s) { *ok = false; return EQF_FILTER_PEAK; }
    if (!strcmp(s, "peak") || !strcmp(s, "bell") || !strcmp(s, "pk"))
        return EQF_FILTER_PEAK;
    if (!strcmp(s, "lowshelf") || !strcmp(s, "low_shelf") || !strcmp(s, "ls"))
        return EQF_FILTER_LOW_SHELF;
    if (!strcmp(s, "highshelf") || !strcmp(s, "high_shelf") || !strcmp(s, "hs"))
        return EQF_FILTER_HIGH_SHELF;
    if (!strcmp(s, "lowpass") || !strcmp(s, "lpf") || !strcmp(s, "lp"))
        return EQF_FILTER_LOWPASS;
    if (!strcmp(s, "highpass") || !strcmp(s, "hpf") || !strcmp(s, "hp"))
        return EQF_FILTER_HIGHPASS;
    if (!strcmp(s, "bandpass") || !strcmp(s, "bpf") || !strcmp(s, "bp"))
        return EQF_FILTER_BANDPASS;
    if (!strcmp(s, "notch") || !strcmp(s, "bandstop"))
        return EQF_FILTER_NOTCH;
    if (!strcmp(s, "allpass") || !strcmp(s, "ap"))
        return EQF_FILTER_ALLPASS;
    if (!strcmp(s, "lowshelf12") || !strcmp(s, "lowshelf_12db"))
        return EQF_FILTER_LOWSHELF_12DB;
    if (!strcmp(s, "highshelf12") || !strcmp(s, "highshelf_12db"))
        return EQF_FILTER_HIGHSHELF_12DB;
    *ok = false;
    return EQF_FILTER_PEAK;
}

static int apply_config(eqf_chain *c, const eqf_json *root)
{
    if (root->type != EQF_JSON_OBJECT) {
        set_err(c, "config root must be an object");
        return EQF_ERR_PARSE;
    }

    /* preamp / post gain */
    double pre_db = eqf_json_obj_number(root, "preamp_db",
                    eqf_json_obj_number(root, "preamp", 0.0));
    double post_db = eqf_json_obj_number(root, "post_gain_db", 0.0);
    if (pre_db < -60.0 || pre_db > 60.0 || post_db < -60.0 || post_db > 60.0) {
        set_err(c, "gain out of range (-60..60 dB)");
        return EQF_ERR_PARSE;
    }

    /* EQ bands */
    const eqf_json *eq = eqf_json_obj_get(root, "eq");
    const eqf_json *filters = eq ? eqf_json_obj_array(eq, "filters") : NULL;
    eqf_band_spec bands[EQF_MAX_BANDS];
    int n_bands = 0;
    if (filters) {
        size_t n = eqf_json_arr_size(filters);
        if (n > EQF_MAX_BANDS) {
            set_err(c, "too many EQ bands (%zu > %d)", n, EQF_MAX_BANDS);
            return EQF_ERR_PARSE;
        }
        for (size_t i = 0; i < n; i++) {
            const eqf_json *f = eqf_json_arr_get(filters, i);
            if (!f)
                continue;
            bool type_ok = false;
            const char *ts = eqf_json_obj_string(f, "type", "peak");
            enum eqf_filter_type t = parse_filter_type(ts, &type_ok);
            if (!type_ok) {
                set_err(c, "filter %zu: unknown type '%s'", i, ts);
                return EQF_ERR_PARSE;
            }
            double freq = eqf_json_obj_number(f, "freq", eqf_json_obj_number(f, "frequency", 1000.0));
            double gain = eqf_json_obj_number(f, "gain_db", eqf_json_obj_number(f, "gain", 0.0));
            double q = eqf_json_obj_number(f, "q", 1.0);
            /* Shelves default to *slope* semantics (cookbook S), matching the
             * Python coefficient designer and the documented config schema.
             * Defaulting to Q here made a shelf band with a "q" field mean
             * something different in the native core than in the fallback
             * reference: up to 0.15 dB of shape error at 6 kHz for a -2 dB
             * shelf, and a different asymptotic shape in the transition band.
             * `use_q: true` still opts into Q semantics explicitly. */
            bool is_shelf = (t == EQF_FILTER_LOW_SHELF ||
                             t == EQF_FILTER_HIGH_SHELF);
            /* Single rule, shared with eqforge/dsp/coeffs.py:
             *   1. an explicit "use_q" wins;
             *   2. otherwise an explicit "slope" means shelf-slope semantics;
             *   3. otherwise shelves default to slope, everything else to Q.
             * The shape parameter is read from whichever field the caller
             * supplied so `q` and `slope` are never silently interchanged. */
            double slope = eqf_json_obj_number(f, "slope", 0.71);
            bool has_slope = eqf_json_obj_has(f, "slope");
            bool use_q = eqf_json_obj_bool(f, "use_q",
                                           has_slope ? false : !is_shelf);
            if (use_q) {
                q = eqf_json_obj_number(f, "q", 1.0);
            } else {
                q = has_slope ? slope : eqf_json_obj_number(f, "q", slope);
            }
            bool enabled = eqf_json_obj_bool(f, "enabled", true);
            const char *chs = eqf_json_obj_string(f, "channel", NULL);
            int ch = -1;
            if (chs) {
                if (!strcmp(chs, "all")) {
                    ch = -1;
                } else {
                    char *endp = NULL;
                    long v = strtol(chs, &endp, 10);
                    if (endp && *endp == '\0' && v >= 0 && (uint32_t)v < c->channels)
                        ch = (int)v;
                    else {
                        set_err(c, "filter %zu: invalid channel '%s'", i, chs);
                        return EQF_ERR_PARSE;
                    }
                }
            }
            if (freq <= 0.0 || gain < -60.0 || gain > 60.0 || q <= 0.0) {
                set_err(c, "filter %zu: parameter out of range", i);
                return EQF_ERR_PARSE;
            }
            bands[n_bands].type = t;
            bands[n_bands].freq = freq;
            bands[n_bands].gain_db = gain;
            bands[n_bands].q = q;
            bands[n_bands].use_q = use_q;
            bands[n_bands].enabled = enabled;
            bands[n_bands].channel = ch;
            n_bands++;
        }
    }

    /* crossfeed */
    eqf_crossfeed_config xcfg;
    eqf_crossfeed_config_default(&xcfg);
    const eqf_json *xfj = eqf_json_obj_get(root, "crossfeed");
    if (xfj) {
        xcfg.enabled = eqf_json_obj_bool(xfj, "enabled", false);
        xcfg.level_db = eqf_json_obj_number(xfj, "level_db", xcfg.level_db);
        xcfg.fc_hz = eqf_json_obj_number(xfj, "fc_hz", eqf_json_obj_number(xfj, "cutoff_hz", xcfg.fc_hz));
    }

    /* convolver */
    bool conv_enabled = false;
    float conv_gain = 1.0f;
    const eqf_json *cvj = eqf_json_obj_get(root, "convolver");
    if (cvj) {
        conv_enabled = eqf_json_obj_bool(cvj, "enabled", false);
        conv_gain = (float)pow(10.0, eqf_json_obj_number(cvj, "gain_db", 0.0) / 20.0);
    }

    /* compressor */
    eqf_compressor_config ccfg;
    eqf_compressor_config_default(&ccfg);
    bool comp_enabled = false;
    const eqf_json *cpj = eqf_json_obj_get(root, "compressor");
    if (cpj) {
        comp_enabled = eqf_json_obj_bool(cpj, "enabled", false);
        ccfg.threshold_db = eqf_json_obj_number(cpj, "threshold_db", ccfg.threshold_db);
        ccfg.ratio = eqf_json_obj_number(cpj, "ratio", ccfg.ratio);
        ccfg.knee_db = eqf_json_obj_number(cpj, "knee_db", ccfg.knee_db);
        ccfg.attack_ms = eqf_json_obj_number(cpj, "attack_ms", ccfg.attack_ms);
        ccfg.release_ms = eqf_json_obj_number(cpj, "release_ms", ccfg.release_ms);
        ccfg.makeup_db = eqf_json_obj_number(cpj, "makeup_db", ccfg.makeup_db);
        ccfg.stereo_link = eqf_json_obj_bool(cpj, "stereo_link", ccfg.stereo_link);
        ccfg.rms_detect = eqf_json_obj_bool(cpj, "rms_detect", ccfg.rms_detect);
        ccfg.rms_window_ms = eqf_json_obj_number(cpj, "rms_window_ms", ccfg.rms_window_ms);
    }

    /* limiter */
    eqf_limiter_config lcfg;
    eqf_limiter_config_default(&lcfg);
    bool lim_enabled = false;
    const eqf_json *lj = eqf_json_obj_get(root, "limiter");
    if (lj) {
        lim_enabled = eqf_json_obj_bool(lj, "enabled", false);
        lcfg.ceiling_db = eqf_json_obj_number(lj, "ceiling_db", lcfg.ceiling_db);
        lcfg.attack_ms = eqf_json_obj_number(lj, "attack_ms", lcfg.attack_ms);
        lcfg.release_ms = eqf_json_obj_number(lj, "release_ms", lcfg.release_ms);
        lcfg.lookahead_ms = eqf_json_obj_number(lj, "lookahead_ms", lcfg.lookahead_ms);
        lcfg.knee_db = eqf_json_obj_number(lj, "knee_db", lcfg.knee_db);
    }

    /* ---- all parsed & validated: commit ---- */

    /* Parameter ramps exist to make live knob moves click-free. Offline
     * renderers disable the fade (eqf_chain_set_fade(c,false)) and therefore
     * must also get ramp-free behaviour, otherwise the first 20 ms of every
     * render differs from the steady-state transfer function and native/offline
     * output stops being sample-exact. */
    double ramp_ms = c->fade_enabled ? 20.0 : 0.0;
    if (eqf_eq_load(&c->eq, bands, n_bands, ramp_ms) != EQF_OK) {
        set_err(c, "failed to load EQ bands");
        return EQF_ERR_STATE;
    }
    c->eq.active = (n_bands > 0);

    if (eqf_crossfeed_configure(&c->crossfeed, &xcfg) != EQF_OK) {
        set_err(c, "invalid crossfeed config");
        return EQF_ERR_PARSE;
    }
    eqf_crossfeed_set_active(&c->crossfeed, xcfg.enabled && c->channels == 2);

    if (eqf_compressor_configure(&c->compressor, &ccfg) != EQF_OK) {
        set_err(c, "invalid compressor config");
        return EQF_ERR_PARSE;
    }
    eqf_compressor_set_active(&c->compressor, comp_enabled);

    if (eqf_limiter_configure(&c->limiter, &lcfg) != EQF_OK) {
        set_err(c, "invalid limiter config");
        return EQF_ERR_PARSE;
    }
    eqf_limiter_set_active(&c->limiter, lim_enabled);

    /* convolver IRs (copy from JSON doc) */
    eqf_convolver_set_active(&c->convolver, false);
    if (conv_enabled && cvj) {
        const eqf_json *ir = eqf_json_obj_get(cvj, "ir");
        if (ir && ir->type == EQF_JSON_ARRAY && eqf_json_arr_size(ir) > 0) {
            const eqf_json *first = eqf_json_arr_get(ir, 0);
            int rc = EQF_OK;
            if (first->type == EQF_JSON_ARRAY) {
                size_t nch = eqf_json_arr_size(ir);
                if (nch > c->channels)
                    nch = c->channels;
                for (size_t ch = 0; ch < nch && rc == EQF_OK; ch++) {
                    const eqf_json *chan_ir = eqf_json_arr_get(ir, ch);
                    size_t nt = eqf_json_arr_size(chan_ir);
                    if (nt == 0 || nt > EQF_MAX_FIR_TAPS) {
                        rc = EQF_ERR_PARSE;
                        break;
                    }
                    float *tmp = (float *)malloc(nt * sizeof(float));
                    if (!tmp) { rc = EQF_ERR_NO_MEMORY; break; }
                    for (size_t k = 0; k < nt; k++)
                        tmp[k] = (float)eqf_json_get_number(eqf_json_arr_get(chan_ir, k), 0.0);
                    rc = eqf_convolver_set_ir(&c->convolver, (int)ch, tmp, (uint32_t)nt);
                    free(tmp);
                }
            } else if (first->type == EQF_JSON_NUMBER) {
                size_t nt = eqf_json_arr_size(ir);
                if (nt > EQF_MAX_FIR_TAPS) {
                    rc = EQF_ERR_PARSE;
                } else {
                    float *tmp = (float *)malloc(nt * sizeof(float));
                    if (!tmp) rc = EQF_ERR_NO_MEMORY;
                    else {
                        for (size_t k = 0; k < nt; k++)
                            tmp[k] = (float)eqf_json_get_number(eqf_json_arr_get(ir, k), 0.0);
                        rc = eqf_convolver_set_ir_all(&c->convolver, tmp, (uint32_t)nt);
                        free(tmp);
                    }
                }
            }
            if (rc == EQF_OK) {
                eqf_convolver_set_gain(&c->convolver, conv_gain);
                eqf_convolver_set_active(&c->convolver, true);
            } else {
                set_err(c, "invalid convolver IR");
                return rc;
            }
        }
    }

    c->preamp = (float)pow(10.0, pre_db / 20.0);
    c->postgain = (float)pow(10.0, post_db / 20.0);

    /* start a short fade-in from silence for click-free switching */
    c->fade_pos = 0;
    c->fading_in = true;

    return EQF_OK;
}

int eqf_chain_configure_json(eqf_chain *c, const char *json, size_t len)
{
    if (!c || !json)
        return EQF_ERR_INVALID_ARG;
    /* len == 0 means "NUL-terminated, measure it", the same convention
     * eqf_json_parse() uses. Storing `len` bytes verbatim made the cached
     * config an empty string for the most common call shape, so
     * eqf_chain_current_config() and every later re-design from the cached
     * config (eqf_chain_set_sample_rate, eqf_chain_set_channels) silently
     * lost the whole graph. */
    if (len == 0)
        len = strlen(json);
    eqf_json *doc = NULL;
    char perr[128] = { 0 };
    int rc = eqf_json_parse(json, len, &doc, perr, sizeof(perr));
    if (rc != EQF_OK) {
        set_err(c, "config JSON parse error: %s", perr);
        return rc;
    }
    rc = apply_config(c, doc);
    eqf_json_free(doc);
    if (rc == EQF_OK) {
        free(c->config_copy);
        c->config_copy = (char *)malloc(len + 1);
        if (c->config_copy) {
            memcpy(c->config_copy, json, len);
            c->config_copy[len] = '\0';
        }
    }
    return rc;
}

int eqf_chain_configure_file(eqf_chain *c, const char *path)
{
    if (!c || !path)
        return EQF_ERR_INVALID_ARG;
    FILE *f = fopen(path, "rb");
    if (!f) {
        set_err(c, "cannot open config file '%s'", path);
        return EQF_ERR_IO;
    }
    fseek(f, 0, SEEK_END);
    long sz = ftell(f);
    fseek(f, 0, SEEK_SET);
    if (sz <= 0 || sz > 64 * 1024 * 1024) {
        fclose(f);
        set_err(c, "config file has unreasonable size %ld", sz);
        return EQF_ERR_IO;
    }
    char *buf = (char *)malloc((size_t)sz + 1);
    if (!buf) {
        fclose(f);
        return EQF_ERR_NO_MEMORY;
    }
    size_t rd = fread(buf, 1, (size_t)sz, f);
    fclose(f);
    buf[rd] = '\0';
    int rc = eqf_chain_configure_json(c, buf, rd);
    free(buf);
    return rc;
}

int eqf_chain_set_sample_rate(eqf_chain *c, uint32_t sample_rate)
{
    if (!c || sample_rate == 0)
        return EQF_ERR_INVALID_ARG;
    if (sample_rate == c->sample_rate)
        return EQF_OK;
    c->sample_rate = sample_rate;
    eqf_eq_set_sample_rate(&c->eq, sample_rate);
    c->crossfeed.sample_rate = sample_rate;
    eqf_crossfeed_configure(&c->crossfeed, &c->crossfeed.cfg);
    c->compressor.sample_rate = sample_rate;
    eqf_compressor_configure(&c->compressor, &c->compressor.cfg);
    c->limiter.sample_rate = sample_rate;
    eqf_limiter_configure(&c->limiter, &c->limiter.cfg);
    eqf_meter_free(&c->meter_in);
    eqf_meter_free(&c->meter_out);
    eqf_meter_init(&c->meter_in, c->channels, sample_rate);
    eqf_meter_init(&c->meter_out, c->channels, sample_rate);
    c->fade_len = FADE_SAMPLES_AT(sample_rate, 10);
    /* Reload EQ design at the new rate if we have a serialized config. */
    if (c->config_copy)
        eqf_chain_configure_json(c, c->config_copy, strlen(c->config_copy));
    return EQF_OK;
}

int eqf_chain_set_channels(eqf_chain *c, uint32_t channels)
{
    if (!c || channels == 0 || channels > EQF_MAX_CHANNELS)
        return EQF_ERR_INVALID_ARG;
    if (channels == c->channels)
        return EQF_OK;
    /* Rebuild everything for the new channel count.
     *
     * `cfg` must be an independent copy: eqf_chain_configure_json() on the new
     * chain frees its *own* config_copy, so handing it the old chain's buffer
     * had it free the string it was still reading from, and the trailing
     * free(cfg) then double-freed it. The old buffer stays owned by `c` and is
     * released as part of the swap below. */
    uint32_t sr = c->sample_rate, mb = c->max_block;
    char *cfg = NULL;
    if (c->config_copy) {
        size_t n = strlen(c->config_copy);
        cfg = (char *)malloc(n + 1);
        if (!cfg)
            return EQF_ERR_NO_MEMORY;
        memcpy(cfg, c->config_copy, n + 1);
    }
    eqf_chain *nc = eqf_chain_new(channels, sr, mb);
    if (!nc) {
        free(cfg);
        return EQF_ERR_NO_MEMORY;
    }
    if (cfg && eqf_chain_configure_json(nc, cfg, strlen(cfg)) != EQF_OK) {
        /* A band that parsed for the old chain can still fail here (e.g. a
         * per-channel band referencing a channel that no longer exists), so
         * build an unconfigured chain rather than leaving a half-applied one. */
        eqf_chain_free(nc);
        nc = eqf_chain_new(channels, sr, mb);
        if (!nc) {
            free(cfg);
            return EQF_ERR_NO_MEMORY;
        }
    }
    free(cfg);
    /* Swap guts.
     * A plain `backup = *c` no longer works: the seqlock counter is an atomic,
     * which is not copyable in C++. The counter is not payload - it is a
     * per-object version tag - so each object keeps its own across the move,
     * while every other member is exchanged byte for byte.
     * Concurrency: like every other eqf_chain_* mutator, this must not run
     * concurrently with eqf_chain_process() on the same chain; the caller
     * (rthost, SPA module) builds a fresh chain instead of calling this when
     * it needs to reconfigure a live graph. */
    unsigned seq_c = EQF_ATOMIC_LOAD(&c->snap_seq, EQF_MO_RELAXED);
    unsigned seq_nc = EQF_ATOMIC_LOAD(&nc->snap_seq, EQF_MO_RELAXED);
    eqf_chain backup;
    memcpy(&backup, c, sizeof backup);
    memcpy(c, nc, sizeof *c);
    memcpy(nc, &backup, sizeof backup);
    EQF_ATOMIC_STORE(&c->snap_seq, seq_c, EQF_MO_RELAXED);
    EQF_ATOMIC_STORE(&nc->snap_seq, seq_nc, EQF_MO_RELAXED);
    /* frees the old (now swapped-out) state, including its config_copy */
    eqf_chain_free(nc);
    return EQF_OK;
}

void eqf_chain_set_fade(eqf_chain *c, bool enabled)
{
    if (!c)
        return;
    c->fade_enabled = enabled;
    if (!enabled)
        c->fading_in = false;
}

void eqf_chain_set_bypass(eqf_chain *c, bool bypass)
{
    if (!c || c->bypass == bypass)
        return;
    c->bypass = bypass;
    c->fade_pos = 0;
    c->fading_in = true; /* reuse fade to crossfade around the switch */
}

bool eqf_chain_get_bypass(const eqf_chain *c)
{
    return c ? c->bypass : true;
}

uint32_t eqf_chain_latency(const eqf_chain *c)
{
    if (!c)
        return 0;
    /* eqf_limiter_latency() already returns 0 when the stage is bypassed, so
     * a chain without a limiter reports no latency and needs no compensation. */
    return eqf_limiter_latency(&c->limiter);
}

const eqf_chain_stats *eqf_chain_snapshot(const eqf_chain *c)
{
    return c ? &c->snap : NULL;
}

int eqf_chain_copy_stats(const eqf_chain *c, eqf_chain_stats *out)
{
    if (!c || !out)
        return EQF_ERR_INVALID_ARG;
    /* Seqlock read side: retry if the audio thread was mid-update. Bounded so
     * a caller can never spin forever; if the bound is hit the caller gets a
     * (possibly slightly stale) consistent copy, never a torn one. */
    for (unsigned attempt = 0; attempt < 64; attempt++) {
        unsigned s1 = EQF_ATOMIC_LOAD(&c->snap_seq, EQF_MO_ACQUIRE);
        if (s1 & 1u)
            continue;
        memcpy(out, &c->snap, sizeof(*out));
        EQF_ATOMIC_FENCE();
        unsigned s2 = EQF_ATOMIC_LOAD(&c->snap_seq, EQF_MO_RELAXED);
        if (s1 == s2)
            return EQF_OK;
    }
    return EQF_ERR_STATE;
}

const eqf_chain_stats *eqf_chain_meters(const eqf_chain *c)
{
    return eqf_chain_snapshot(c);
}

const eqf_eq *eqf_chain_eq(const eqf_chain *c)
{
    return c ? &c->eq : NULL;
}

const char *eqf_chain_current_config(const eqf_chain *c)
{
    return c ? c->config_copy : NULL;
}

int eqf_chain_process(eqf_chain *c, float *const *in, float *const *out, uint32_t frames)
{
    if (!c || !in || !out || frames == 0)
        return EQF_ERR_INVALID_ARG;
    if (frames > c->max_block) {
        /* Silently truncating here made callers believe the whole buffer had
         * been processed (the tail came back untouched). Refuse instead. */
        set_err(c, "block of %u frames exceeds max_block %u", frames,
                c->max_block);
        return EQF_ERR_INVALID_ARG;
    }
    for (uint32_t ch = 0; ch < c->channels; ch++) {
        if (!in[ch] || !out[ch]) {
            set_err(c, "null channel pointer");
            return EQF_ERR_INVALID_ARG;
        }
    }

    const float *const *cin = (const float *const *)in;
    eqf_meter_update(&c->meter_in, cin, frames);

    /* copy into scratch for in-place processing */
    for (uint32_t ch = 0; ch < c->channels; ch++) {
        memcpy(c->scratch[ch], in[ch], frames * sizeof(float));
        c->ptrs_out[ch] = c->scratch[ch];
    }

    if (!c->bypass) {
        float pre = c->preamp;
        if (pre != 1.0f) {
            for (uint32_t ch = 0; ch < c->channels; ch++)
                for (uint32_t i = 0; i < frames; i++)
                    c->scratch[ch][i] *= pre;
        }

        eqf_eq_process(&c->eq, c->ptrs_out, frames);

        if (c->channels == 2)
            eqf_crossfeed_process(&c->crossfeed, c->scratch[0], c->scratch[1], frames);

        eqf_convolver_process(&c->convolver, c->ptrs_out, frames);
        eqf_compressor_process(&c->compressor, c->ptrs_out, frames);
        eqf_limiter_process(&c->limiter, c->ptrs_out, frames);

        float post = c->postgain;
        if (post != 1.0f) {
            for (uint32_t ch = 0; ch < c->channels; ch++)
                for (uint32_t i = 0; i < frames; i++)
                    c->scratch[ch][i] *= post;
        }
    }

    /* fade-in after config changes/bypass flips (multiplicative ramp from 0) */
    if (c->fading_in && c->fade_enabled) {
        uint32_t n = c->fade_len;
        for (uint32_t i = 0; i < frames; i++) {
            uint32_t p = c->fade_pos + i;
            float g = p < n ? (float)(p + 1) / (float)n : 1.0f;
            for (uint32_t ch = 0; ch < c->channels; ch++)
                out[ch][i] = c->scratch[ch][i] * g;
        }
        if (c->fade_pos + frames >= n)
            c->fading_in = false;
        c->fade_pos += frames;
    } else {
        for (uint32_t ch = 0; ch < c->channels; ch++)
            memcpy(out[ch], c->scratch[ch], frames * sizeof(float));
    }

    const float *const *cout = (const float *const *)out;
    eqf_meter_update(&c->meter_out, cout, frames);

    /* publish snapshot (seqlock write side; odd while in progress) */
    eqf_chain_stats *s = &c->snap;
    unsigned seq = EQF_ATOMIC_LOAD(&c->snap_seq, EQF_MO_RELAXED);
    EQF_ATOMIC_STORE(&c->snap_seq, seq + 1, EQF_MO_RELAXED);
    EQF_ATOMIC_FENCE();
    for (uint32_t ch = 0; ch < c->channels; ch++) {
        s->in_peak[ch] = c->meter_in.peak[ch];
        s->in_rms[ch] = c->meter_in.rms[ch];
        s->out_peak[ch] = c->meter_out.peak[ch];
        s->out_rms[ch] = c->meter_out.rms[ch];
        s->out_true_peak[ch] = c->meter_out.true_peak[ch];
    }
    s->momentary_lufs = c->meter_out.momentary_lufs;
    s->limiter_gain_db = eqf_limiter_gain_db(&c->limiter);
    s->limiter_true_peak = eqf_limiter_true_peak(&c->limiter);
    s->compressor_gr_db = eqf_compressor_gain_reduction_db(&c->compressor);
    s->in_clipped = c->meter_in.clipped;
    s->out_clipped = c->meter_out.clipped;
    s->latency_frames = eqf_chain_latency(c);
    s->channels = c->channels;
    EQF_ATOMIC_FENCE();
    EQF_ATOMIC_STORE(&c->snap_seq, seq + 2, EQF_MO_RELAXED);
    return EQF_OK;
}

/* ---------- facade helpers ---------- */

const char *eqf_version_string(void)
{
    return EQF_VERSION_STRING;
}

uint32_t eqf_abi_version(void)
{
    return EQF_ABI_VERSION;
}

eqf_chain *eqf_chain_open_file(uint32_t channels, uint32_t sample_rate,
                               uint32_t max_block, const char *config_path)
{
    eqf_chain *c = eqf_chain_new(channels, sample_rate, max_block);
    if (!c)
        return NULL;
    if (config_path && config_path[0]) {
        int rc = eqf_chain_configure_file(c, config_path);
        if (rc != EQF_OK) {
            eqf_chain_free(c);
            return NULL;
        }
    }
    return c;
}
