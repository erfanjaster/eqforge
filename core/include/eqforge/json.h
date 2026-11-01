/* EQForge native core - minimal dependency-free JSON parser.
 *
 * Supports the full JSON grammar (objects, arrays, strings with escapes,
 * numbers, booleans, null). Parsing is single-pass into a preallocated
 * arena, so it is deterministic and suitable for control-thread use.
 *
 * SPDX-License-Identifier: MIT
 */
#ifndef EQFORGE_JSON_H
#define EQFORGE_JSON_H

#include <stdbool.h>
#include <stddef.h>

#include "types.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    EQF_JSON_NULL,
    EQF_JSON_BOOL,
    EQF_JSON_NUMBER,
    EQF_JSON_STRING,
    EQF_JSON_ARRAY,
    EQF_JSON_OBJECT,
} eqf_json_type;

typedef struct eqf_json eqf_json;

struct eqf_json {
    eqf_json_type type;
    /* scalars */
    double number;
    bool boolean;
    char *string;          /* NUL-terminated, owned by arena */
    size_t string_len;
    /* containers */
    eqf_json **items;      /* array elements or object values */
    char **keys;           /* object keys (NULL for arrays) */
    size_t count;
};

/* Parse `len` bytes (or strlen if len == 0). On success *out receives a
 * document that must be released with eqf_json_free(). On failure returns
 * EQF_ERR_PARSE and, if err != NULL, a human readable message (static or
 * into errbuf). */
int eqf_json_parse(const char *text, size_t len, eqf_json **out,
                   char *errbuf, size_t errbuf_len);

void eqf_json_free(eqf_json *doc);

/* Convenience accessors. All return NULL/defaults when missing or when the
 * type does not match; they never fail hard. */
const eqf_json *eqf_json_obj_get(const eqf_json *obj, const char *key);
const eqf_json *eqf_json_arr_get(const eqf_json *arr, size_t index);
size_t eqf_json_arr_size(const eqf_json *arr);

double eqf_json_get_number(const eqf_json *node, double def);
bool eqf_json_get_bool(const eqf_json *node, bool def);
const char *eqf_json_get_string(const eqf_json *node, const char *def);

/* Object field helpers. */
double eqf_json_obj_number(const eqf_json *obj, const char *key, double def);
bool eqf_json_obj_bool(const eqf_json *obj, const char *key, bool def);
const char *eqf_json_obj_string(const eqf_json *obj, const char *key, const char *def);
const eqf_json *eqf_json_obj_array(const eqf_json *obj, const char *key);

#ifdef __cplusplus
}
#endif

#endif /* EQFORGE_JSON_H */
