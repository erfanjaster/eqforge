# Profile Format Specification

EQForge profiles are UTF-8 JSON documents. This file is the normative
reference; `eqforge/profiles/schema.py` implements validation and
`eqforge/profiles/versioning.py` implements migrations.

## Example

```json
{
  "format": "eqforge.profile",
  "format_version": 2,
  "id": "hd650.office",
  "name": "HD650 — Office",
  "description": "AutoEQ correction + gentle late-night tilt",
  "tags": ["headphone", "sennheiser", "hd650"],
  "author": "you",
  "created": 1758000000.0,
  "modified": 1758000000.0,

  "extends": ["headphone-base"],

  "preamp": { "mode": "auto" },

  "eq": {
    "filters_op": "append",
    "filters": [
      { "type": "peak", "freq": 105.0, "gain_db": 5.1, "q": 1.10,
        "enabled": true, "channel": "all", "name": "bass" },
      { "type": "highshelf", "freq": 8500, "gain_db": -1.8, "q": 0.7,
        "use_q": false, "slope": 0.71 },
      { "type": "peak", "freq": 2800, "gain_db": -1.2, "q": 3.0,
        "channel": 1 }
    ]
  },

  "crossfeed":  { "enabled": true, "level_db": -6.5, "fc_hz": 700 },
  "compressor": { "enabled": false },
  "convolver":  { "enabled": false },
  "limiter": {
    "enabled": true, "ceiling_db": -1.0,
    "attack_ms": 5, "release_ms": 60, "lookahead_ms": 1.5, "knee_db": 3
  },

  "loudness": { "target_lufs": -16.0, "true_peak_db": -1.0 },

  "matching": {
    "devices": [
      { "profile": "hd650.office",
        "match": { "name": "*HD650*", "kind": "usb" },
        "priority": 10 }
    ],
    "apps": [
      { "profile": "voice", "match": { "binary": "*zoom*" }, "priority": 5 }
    ]
  },

  "analysis": { "auto_preamp": true }
}
```

## Field reference

| Field | Type | Notes |
|---|---|---|
| `format` | string | always `"eqforge.profile"` |
| `format_version` | int | current: **2**; older docs are auto-migrated |
| `id` | string | `[A-Za-z0-9][A-Za-z0-9._-]*`, ≤128 chars; unique; user profiles shadow builtins |
| `name`, `description`, `author` | string | display metadata |
| `tags` | string[] | freeform, used by GUI/search |
| `created`, `modified` | number | unix seconds |
| `extends` | string[] | profile ids to inherit, in order (max chain depth 8; cycles rejected) |
| `preamp.mode` | `"auto"`\|`"manual"` | **auto**: `−Σ(positive enabled gains)` — guarantees the EQ alone cannot exceed unity at any frequency |
| `preamp.gain_db` | number | manual mode only, −60…+60 |
| `eq.filters[]` | array | ≤ 64 entries |
| `eq.filters_op` | `"replace"`\|`"append"` | inheritance behavior of the list (default replace) |
| `crossfeed` | object | headphone virtualization; `level_db` −30…0, `fc_hz` 50…4000 |
| `compressor` | object | feed-forward; `ratio` 1…100, `threshold_db` −100…0 |
| `convolver` | object | FIR correction; `ir` inline **or** `ir_file` (WAV, resolved at dsp-config time) |
| `limiter` | object | look-ahead true-peak; `ceiling_db` −60…0 |
| `loudness.target_lufs` | number\|null | offline `apply` normalizes to this when set |
| `matching.devices[]`, `matching.apps[]` | array | see below |
| unknown keys | any | preserved, ignored by the engine (forward compatibility) |

### Filter object

| Field | Type | Range / values |
|---|---|---|
| `type` | string | `peak` `lowshelf` `highshelf` `lowpass` `highpass` `bandpass` `notch` `allpass` `lowshelf12` `highshelf12` |
| `freq` | number | 1 … 1e6 Hz (clamped to Nyquist at design time) |
| `gain_db` | number | −48…48 (>24 warns); ignored for pass/notch types |
| `q` | number | 0.0001…200 |
| `use_q` | bool | default: true for peak/pass, false for shelves |
| `slope` | number | shelf slope S ∈ (0,1] when `use_q:false` |
| `enabled` | bool | default true |
| `channel` | `"all"` \| int | per-channel filtering (0-based) |
| `name` | string | optional label (shown in GUI/table) |

### Matching rules

```json
{ "profile": "<id>", "match": { "<identity-key>": "<glob-or-substring>" },
  "priority": 10 }
```

Identity keys for devices: `name`, `node_name`, `description`, `kind`
(`bluetooth` `usb` `hdmi` `headphones` `speakers` `other`), `alsa_card`,
`bluez_address`, `bluez_name`, `serial`, `vendor`. For apps: `name`,
`binary`, `node_name`. Values with `*`/`?` are globs, otherwise
case-insensitive substring. All keys in a rule must match (AND); highest
`priority` wins, then most specific.

## Inheritance semantics

`extends: [p1, p2]` merges p1, then p2, then this profile (deep merge of
objects). Lists replace by default; `eq.filters` obeys `filters_op`:

* `replace` (default) — child's list wins outright
* `append` — parent filters followed by child filters

Recommended layering:

```
flat                (safety limiter, nothing else)
└── headphone-base  (gentle Harman-ish tilt)
    └── hd650-autoeq    (imported per-unit correction)
        └── hd650.late-night (adds compression for the apartment)
```

## Validation

`eqforge validate <id|file>` reports **errors** (reject) and **warnings**
(load anyway). Errors include: unknown filter types, out-of-range values,
bad ids, circular/too-deep `extends`, limiter ceiling > 0 dB, malformed
matching rules. Warnings include: extreme boosts (>24 dB), gain on filter
types that ignore it, useless sub-25 Hz high-pass.

Rejected configurations never reach the DSP core; additionally the C core
re-validates its dsp-config independently and refuses unsafe values.

## Resolved dsp-config

The contract with the native engine (what `active-dsp.json` contains, what
`libeqforge` parses, what `--resolved` prints):

```json
{
  "preamp_db": -6.5,
  "eq": { "filters": [ { "type": "peak", "freq": 105.0, "gain_db": 5.1,
                          "q": 1.1, "enabled": true } ] },
  "crossfeed":  { "enabled": true, "level_db": -6.5, "fc_hz": 700.0 },
  "compressor": { "enabled": false, "threshold_db": -18.0, "ratio": 3.0,
                  "knee_db": 6.0, "attack_ms": 10.0, "release_ms": 120.0,
                  "makeup_db": 0.0, "stereo_link": true, "rms_detect": false },
  "limiter":    { "enabled": true, "ceiling_db": -1.0, "attack_ms": 5.0,
                  "release_ms": 60.0, "lookahead_ms": 1.5, "knee_db": 3.0 },
  "convolver":  { "enabled": false },
  "post_gain_db": 0.0
}
```

Transformations from profile → dsp-config:

* inheritance resolved; disabled filters dropped; `channel:"all"` kept as-is
  (native expands it), integer channels serialized as strings
* `preamp.mode:"auto"` → computed headroom value
* `convolver.ir_file` → inlined taps (WAV decoded, peak-normalized,
  ≤ 8192 taps/channel)
* `matching`, `loudness`, `analysis`, metadata stay in the Python layer

## Version history

* **v2** (current): chain stages, matching rules, preamp modes, filters_op,
  per-channel filters.
* **v1**: flat `filters` list, `preamp_db`, `device_match`. Migrated
  automatically on load (`versioning.py::_migrate_1_to_2`).
