# Configuration Reference

## Files & locations (XDG)

| Path | Purpose |
|---|---|
| `~/.config/eqforge/config.json` | user configuration (below) |
| `~/.config/eqforge/profiles/*.json` | user profiles |
| `~/.config/eqforge/active-dsp.json` | resolved dsp-config for native hosts (managed — don't hand-edit) |
| `~/.local/share/eqforge/imports/` | import scratch/history |
| `~/.local/state/eqforge/logs/eqforge.log` | rotating log (2 MB × 3) |
| `$XDG_RUNTIME_DIR/eqforge/daemon.sock` | daemon JSON-RPC socket (0600) |
| `$XDG_RUNTIME_DIR/eqforge/rt.sock` | RT host control socket (0600) |
| `$XDG_RUNTIME_DIR/eqforge/gui.port` | current GUI port |

Environment overrides: `EQFORGE_LIB` (path to libeqforge.so), `EQFORGE_RT`
(path to eqforge-rt), `EQFORGE_CONFIG` (config path for native hosts),
`EQFORGE_DEBUG=1` (verbose console logging), `NO_COLOR=1`.

## `config.json`

```json
{
  "profile_a": "flat",
  "profile_b": "flat",
  "active_slot": "A",
  "bypass": false,
  "auto_switch": false,
  "rules": [
    { "profile": "gaming",
      "match": { "binary": "steam*", "name": "*Game*" },
      "priority": 10 },
    { "profile": "hd650.office",
      "match": { "bluez_address": "AA:BB:CC:DD:EE:FF" },
      "priority": 20 }
  ],
  "gui": { "port": 0, "open_browser": true },
  "notifications": true
}
```

| Key | Default | Meaning |
|---|---|---|
| `profile_a` / `profile_b` | `"flat"` | A/B slot assignments (persisted across restarts) |
| `active_slot` | `"A"` | which slot is live |
| `bypass` | `false` | engine bypass state |
| `auto_switch` | `false` | allow device-rule profile switching |
| `rules` | `[]` | global matching rules (merged with per-profile rules) |
| `gui.port` | `0` | fixed GUI port (0 = ephemeral) |
| `notifications` | `true` | desktop notifications on switches/warnings |

The file is rewritten atomically (tmp + rename). A corrupt `config.json` is
quarantined to `config.broken.bak` and defaults are used — the daemon always
starts.

## Daemon JSON-RPC (Unix socket, line-delimited)

```sh
echo '{"method":"status"}' | socat - UNIX-CONNECT:$XDG_RUNTIME_DIR/eqforge/daemon.sock
```

| Method | Params | Result |
|---|---|---|
| `ping` | — | `{ok, version, rpc}` |
| `status` | — | slots, active profile, bypass, rt telemetry |
| `set_profile` | `{profile, slot?}` | new status |
| `bypass` | `{value?}` (absent = toggle) | new status |
| `ab` | `{slot?}` (absent = toggle) | new status |
| `list_profiles` | — | profile entries |
| `resolve_profile` | `{profile}` | `{resolved, dsp}` |
| `save_profile` | `{profile}` | `{path}` |
| `delete_profile` | `{profile}` | `{ok}` |
| `devices` | — | sinks/sources + default |
| `streams` | — | app streams |
| `auto_switch` | `{value?}` | `{auto_switch}` |
| `apply_rules_now` | — | `{switched, profile?}` |
| `analyze_file` | `{path, profile?}` | report + advice |
| `rt_command` | `{cmd}` | RT host reply |

Errors return `{"ok": false, "error": "...", "hint": "..."}` with a stable
exit-code-compatible `code`. The same methods are available over HTTP:
`GET /api/<method>` (no params) and `POST /api/<method>` (JSON body);
`GET /api/events` is the SSE stream (`meters`, `profile_changed`,
`graph_changed`, `profiles_changed`).

## Logging

Console: concise (`I`/`W`/`E` level prefix); `-v` for timestamps+loggers.
File: `~/.local/state/eqforge/logs/eqforge.log`, rotating, DEBUG level.
Native hosts log to `~/.local/state/eqforge/logs/rt.out` (RT) and the
PipeWire journal (SPA module).
