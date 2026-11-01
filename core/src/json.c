/* EQForge native core - minimal JSON parser implementation.
 *
 * SPDX-License-Identifier: MIT
 */
#include "eqforge/json.h"

#include <ctype.h>
#include <stdio.h>

int eqf_json_registry_put(eqf_json *doc, void *arena_head);
#include <stdlib.h>
#include <string.h>

/* ---------- eqf_arena ---------- */

typedef struct arena_chunk {
    struct arena_chunk *next;
    size_t used;
    size_t size;
    char data[];
} arena_chunk;

typedef struct {
    arena_chunk *head;
} eqf_arena;

static void arena_init(eqf_arena *a) { a->head = NULL; }

static void *arena_alloc(eqf_arena *a, size_t n)
{
    n = (n + 15u) & ~(size_t)15u;
    arena_chunk *c = a->head;
    if (!c || c->size - c->used < n) {
        size_t sz = n + 4096;
        if (sz < 16384)
            sz = 16384;
        arena_chunk *nc = (arena_chunk *)malloc(sizeof(arena_chunk) + sz);
        if (!nc)
            return NULL;
        nc->next = a->head;
        nc->used = 0;
        nc->size = sz;
        a->head = nc;
        c = nc;
    }
    void *p = c->data + c->used;
    c->used += n;
    return p;
}

static void arena_free(eqf_arena *a)
{
    arena_chunk *c = a->head;
    while (c) {
        arena_chunk *next = c->next;
        free(c);
        c = next;
    }
    a->head = NULL;
}

/* ---------- parser ---------- */

typedef struct {
    const char *p;
    const char *start;
    const char *end;
    eqf_arena ar;
    char err[128];
    int depth;
} parser;

#define MAX_DEPTH 64

static void perr(parser *ps, const char *msg)
{
    if (ps->err[0] == '\0')
        snprintf(ps->err, sizeof(ps->err), "%s (byte offset %ld)", msg,
                 (long)(ps->p - ps->start));
}

static eqf_json *new_node(parser *ps, eqf_json_type t)
{
    eqf_json *n = (eqf_json *)arena_alloc(&ps->ar, sizeof(eqf_json));
    if (!n)
        return NULL;
    memset(n, 0, sizeof(*n));
    n->type = t;
    return n;
}

static void skip_ws(parser *ps)
{
    while (ps->p < ps->end) {
        char c = *ps->p;
        if (c == ' ' || c == '\t' || c == '\n' || c == '\r')
            ps->p++;
        else
            break;
    }
}

static eqf_json *parse_value(parser *ps);

static int parse_hex4(const char *s, unsigned *out)
{
    unsigned v = 0;
    for (int i = 0; i < 4; i++) {
        char c = s[i];
        v <<= 4;
        if (c >= '0' && c <= '9')
            v |= (unsigned)(c - '0');
        else if (c >= 'a' && c <= 'f')
            v |= (unsigned)(c - 'a' + 10);
        else if (c >= 'A' && c <= 'F')
            v |= (unsigned)(c - 'A' + 10);
        else
            return EQF_ERR_PARSE;
    }
    *out = v;
    return EQF_OK;
}

static char *parse_string_raw(parser *ps, size_t *out_len)
{
    if (ps->p >= ps->end || *ps->p != '"') {
        perr(ps, "expected string");
        return NULL;
    }
    ps->p++;
    const char *start = ps->p;
    size_t cap = (size_t)(ps->end - start) + 1;
    char *buf = (char *)arena_alloc(&ps->ar, cap);
    if (!buf)
        return NULL;
    size_t len = 0;
    while (ps->p < ps->end) {
        char c = *ps->p++;
        if (c == '"') {
            buf[len] = '\0';
            if (out_len)
                *out_len = len;
            return buf;
        }
        if (c == '\\') {
            if (ps->p >= ps->end)
                break;
            char e = *ps->p++;
            switch (e) {
            case '"': buf[len++] = '"'; break;
            case '\\': buf[len++] = '\\'; break;
            case '/': buf[len++] = '/'; break;
            case 'b': buf[len++] = '\b'; break;
            case 'f': buf[len++] = '\f'; break;
            case 'n': buf[len++] = '\n'; break;
            case 'r': buf[len++] = '\r'; break;
            case 't': buf[len++] = '\t'; break;
            case 'u': {
                if (ps->p + 4 > ps->end) {
                    perr(ps, "bad \\u escape");
                    return NULL;
                }
                unsigned cp;
                if (parse_hex4(ps->p, &cp) != EQF_OK) {
                    perr(ps, "bad \\u escape");
                    return NULL;
                }
                ps->p += 4;
                /* surrogate pair */
                if (cp >= 0xD800 && cp <= 0xDBFF && ps->p + 6 <= ps->end &&
                    ps->p[0] == '\\' && ps->p[1] == 'u') {
                    unsigned lo;
                    if (parse_hex4(ps->p + 2, &lo) == EQF_OK &&
                        lo >= 0xDC00 && lo <= 0xDFFF) {
                        cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
                        ps->p += 6;
                    }
                }
                /* encode UTF-8 */
                if (cp < 0x80) {
                    buf[len++] = (char)cp;
                } else if (cp < 0x800) {
                    buf[len++] = (char)(0xC0 | (cp >> 6));
                    buf[len++] = (char)(0x80 | (cp & 0x3F));
                } else if (cp < 0x10000) {
                    buf[len++] = (char)(0xE0 | (cp >> 12));
                    buf[len++] = (char)(0x80 | ((cp >> 6) & 0x3F));
                    buf[len++] = (char)(0x80 | (cp & 0x3F));
                } else {
                    buf[len++] = (char)(0xF0 | (cp >> 18));
                    buf[len++] = (char)(0x80 | ((cp >> 12) & 0x3F));
                    buf[len++] = (char)(0x80 | ((cp >> 6) & 0x3F));
                    buf[len++] = (char)(0x80 | (cp & 0x3F));
                }
                break;
            }
            default:
                perr(ps, "bad escape");
                return NULL;
            }
        } else {
            buf[len++] = c;
        }
    }
    perr(ps, "unterminated string");
    return NULL;
}

static eqf_json *parse_string(parser *ps)
{
    size_t len = 0;
    char *s = parse_string_raw(ps, &len);
    if (!s)
        return NULL;
    eqf_json *n = new_node(ps, EQF_JSON_STRING);
    if (!n)
        return NULL;
    n->string = s;
    n->string_len = len;
    return n;
}

static eqf_json *parse_number(parser *ps)
{
    const char *start = ps->p;
    char tmp[64];
    size_t i = 0;
    while (ps->p < ps->end && i + 1 < sizeof(tmp)) {
        char c = *ps->p;
        if ((c >= '0' && c <= '9') || c == '-' || c == '+' || c == '.' ||
            c == 'e' || c == 'E') {
            tmp[i++] = c;
            ps->p++;
        } else {
            break;
        }
    }
    tmp[i] = '\0';
    char *endp = NULL;
    double v = strtod(tmp, &endp);
    if (endp == tmp || i == 0) {
        ps->p = start;
        perr(ps, "bad number");
        return NULL;
    }
    eqf_json *n = new_node(ps, EQF_JSON_NUMBER);
    if (!n)
        return NULL;
    n->number = v;
    return n;
}

static eqf_json *parse_array(parser *ps)
{
    ps->p++; /* '[' */
    eqf_json *n = new_node(ps, EQF_JSON_ARRAY);
    if (!n)
        return NULL;
    size_t cap = 8;
    n->items = (eqf_json **)arena_alloc(&ps->ar, cap * sizeof(eqf_json *));
    if (!n->items)
        return NULL;
    skip_ws(ps);
    if (ps->p < ps->end && *ps->p == ']') {
        ps->p++;
        return n;
    }
    for (;;) {
        skip_ws(ps);
        eqf_json *v = parse_value(ps);
        if (!v)
            return NULL;
        if (n->count == cap) {
            size_t ncap = cap * 2;
            eqf_json **ni = (eqf_json **)arena_alloc(&ps->ar, ncap * sizeof(eqf_json *));
            if (!ni)
                return NULL;
            memcpy(ni, n->items, n->count * sizeof(eqf_json *));
            n->items = ni;
            cap = ncap;
        }
        n->items[n->count++] = v;
        skip_ws(ps);
        if (ps->p < ps->end && *ps->p == ',') {
            ps->p++;
            continue;
        }
        if (ps->p < ps->end && *ps->p == ']') {
            ps->p++;
            return n;
        }
        perr(ps, "expected ',' or ']' in array");
        return NULL;
    }
}

static eqf_json *parse_object(parser *ps)
{
    ps->p++; /* '{' */
    eqf_json *n = new_node(ps, EQF_JSON_OBJECT);
    if (!n)
        return NULL;
    size_t cap = 8;
    n->items = (eqf_json **)arena_alloc(&ps->ar, cap * sizeof(eqf_json *));
    n->keys = (char **)arena_alloc(&ps->ar, cap * sizeof(char *));
    if (!n->items || !n->keys)
        return NULL;
    skip_ws(ps);
    if (ps->p < ps->end && *ps->p == '}') {
        ps->p++;
        return n;
    }
    for (;;) {
        skip_ws(ps);
        size_t klen = 0;
        char *key = parse_string_raw(ps, &klen);
        if (!key)
            return NULL;
        skip_ws(ps);
        if (ps->p >= ps->end || *ps->p != ':') {
            perr(ps, "expected ':' in object");
            return NULL;
        }
        ps->p++;
        skip_ws(ps);
        eqf_json *v = parse_value(ps);
        if (!v)
            return NULL;
        if (n->count == cap) {
            size_t ncap = cap * 2;
            eqf_json **ni = (eqf_json **)arena_alloc(&ps->ar, ncap * sizeof(eqf_json *));
            char **nk = (char **)arena_alloc(&ps->ar, ncap * sizeof(char *));
            if (!ni || !nk)
                return NULL;
            memcpy(ni, n->items, n->count * sizeof(eqf_json *));
            memcpy(nk, n->keys, n->count * sizeof(char *));
            n->items = ni;
            n->keys = nk;
            cap = ncap;
        }
        n->keys[n->count] = key;
        n->items[n->count] = v;
        n->count++;
        skip_ws(ps);
        if (ps->p < ps->end && *ps->p == ',') {
            ps->p++;
            continue;
        }
        if (ps->p < ps->end && *ps->p == '}') {
            ps->p++;
            return n;
        }
        perr(ps, "expected ',' or '}' in object");
        return NULL;
    }
}

static eqf_json *parse_value(parser *ps)
{
    if (++ps->depth > MAX_DEPTH) {
        perr(ps, "document nested too deeply");
        return NULL;
    }
    skip_ws(ps);
    if (ps->p >= ps->end) {
        perr(ps, "unexpected end of input");
        ps->depth--;
        return NULL;
    }
    char c = *ps->p;
    eqf_json *n = NULL;
    if (c == '{')
        n = parse_object(ps);
    else if (c == '[')
        n = parse_array(ps);
    else if (c == '"')
        n = parse_string(ps);
    else if (c == 't' && (size_t)(ps->end - ps->p) >= 4 && !memcmp(ps->p, "true", 4)) {
        ps->p += 4;
        n = new_node(ps, EQF_JSON_BOOL);
        if (n)
            n->boolean = true;
    } else if (c == 'f' && (size_t)(ps->end - ps->p) >= 5 && !memcmp(ps->p, "false", 5)) {
        ps->p += 5;
        n = new_node(ps, EQF_JSON_BOOL);
        if (n)
            n->boolean = false;
    } else if (c == 'n' && (size_t)(ps->end - ps->p) >= 4 && !memcmp(ps->p, "null", 4)) {
        ps->p += 4;
        n = new_node(ps, EQF_JSON_NULL);
    } else if (c == '-' || (c >= '0' && c <= '9'))
        n = parse_number(ps);
    else
        perr(ps, "unexpected character");
    ps->depth--;
    return n;
}

int eqf_json_parse(const char *text, size_t len, eqf_json **out,
                   char *errbuf, size_t errbuf_len)
{
    if (!text || !out)
        return EQF_ERR_INVALID_ARG;
    if (len == 0)
        len = strlen(text);

    parser ps;
    memset(&ps, 0, sizeof(ps));
    ps.p = text;
    ps.start = text;
    ps.end = text + len;
    arena_init(&ps.ar);

    eqf_json *doc = parse_value(&ps);
    skip_ws(&ps);
    if (doc && ps.p != ps.end) {
        perr(&ps, "trailing data after JSON value");
        doc = NULL;
    }
    if (!doc) {
        if (errbuf && errbuf_len)
            snprintf(errbuf, errbuf_len, "%s", ps.err[0] ? ps.err : "parse error");
        arena_free(&ps.ar);
        return EQF_ERR_PARSE;
    }
    /* Tie the eqf_arena lifetime to the returned document through a small
     * registry (parse/free are expected to run on a control thread). */
    if (eqf_json_registry_put(doc, ps.ar.head) != EQF_OK) {
        arena_free(&ps.ar);
        return EQF_ERR_NO_MEMORY;
    }
    *out = doc;
    return EQF_OK;
}

/* ---------- registry to tie eqf_arena lifetime to document ---------- */

typedef struct reg_entry {
    struct reg_entry *next;
    eqf_json *doc;
    arena_chunk *eqf_arena;
} reg_entry;

static reg_entry *g_registry = NULL;

int eqf_json_registry_put(eqf_json *doc, void *arena_head)
{
    reg_entry *e = (reg_entry *)malloc(sizeof(reg_entry));
    if (!e)
        return EQF_ERR_NO_MEMORY;
    e->doc = doc;
    e->eqf_arena = (arena_chunk *)arena_head;
    e->next = g_registry;
    g_registry = e;
    return EQF_OK;
}

static reg_entry *registry_take(eqf_json *doc)
{
    reg_entry **pp = &g_registry;
    while (*pp) {
        if ((*pp)->doc == doc) {
            reg_entry *e = *pp;
            *pp = e->next;
            return e;
        }
        pp = &(*pp)->next;
    }
    return NULL;
}

void eqf_json_free(eqf_json *doc)
{
    if (!doc)
        return;
    reg_entry *e = registry_take(doc);
    if (e) {
        eqf_arena a;
        a.head = e->eqf_arena;
        arena_free(&a);
        free(e);
    }
}

/* ---------- accessors ---------- */

const eqf_json *eqf_json_obj_get(const eqf_json *obj, const char *key)
{
    if (!obj || obj->type != EQF_JSON_OBJECT || !key)
        return NULL;
    for (size_t i = 0; i < obj->count; i++) {
        if (obj->keys[i] && strcmp(obj->keys[i], key) == 0)
            return obj->items[i];
    }
    return NULL;
}

const eqf_json *eqf_json_arr_get(const eqf_json *arr, size_t index)
{
    if (!arr || arr->type != EQF_JSON_ARRAY || index >= arr->count)
        return NULL;
    return arr->items[index];
}

size_t eqf_json_arr_size(const eqf_json *arr)
{
    if (!arr || arr->type != EQF_JSON_ARRAY)
        return 0;
    return arr->count;
}

double eqf_json_get_number(const eqf_json *node, double def)
{
    if (!node || node->type != EQF_JSON_NUMBER)
        return def;
    return node->number;
}

bool eqf_json_get_bool(const eqf_json *node, bool def)
{
    if (!node || node->type != EQF_JSON_BOOL)
        return def;
    return node->boolean;
}

const char *eqf_json_get_string(const eqf_json *node, const char *def)
{
    if (!node || node->type != EQF_JSON_STRING)
        return def;
    return node->string;
}

double eqf_json_obj_number(const eqf_json *obj, const char *key, double def)
{
    return eqf_json_get_number(eqf_json_obj_get(obj, key), def);
}

bool eqf_json_obj_bool(const eqf_json *obj, const char *key, bool def)
{
    return eqf_json_get_bool(eqf_json_obj_get(obj, key), def);
}

const char *eqf_json_obj_string(const eqf_json *obj, const char *key, const char *def)
{
    return eqf_json_get_string(eqf_json_obj_get(obj, key), def);
}

const eqf_json *eqf_json_obj_array(const eqf_json *obj, const char *key)
{
    const eqf_json *n = eqf_json_obj_get(obj, key);
    if (!n || n->type != EQF_JSON_ARRAY)
        return NULL;
    return n;
}
