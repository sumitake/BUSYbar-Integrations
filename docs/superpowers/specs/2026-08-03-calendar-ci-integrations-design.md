# BUSY Bar Integrations v1 — Design

**Date:** 2026-08-03
**Status:** Approved design, pending spec review
**Scope:** Calendar countdown + CI status integrations, shared device client, public-repo scaffolding

## Overview

`busybar-integrations` is a public monorepo of small, forkable integrations for the
[BUSY Bar](https://busy.app) device, driven over its local HTTP API (USB-Ethernet
default `10.0.4.20`, also reachable over Wi-Fi LAN). v1 ships two integrations plus
a shared Python client library.

The BUSY Bar firmware arbitrates its 72×16 LED display itself: each draw request
carries an `application_name` and a priority, and a lower-priority draw while a
higher-priority app is active returns HTTP 409. Integrations therefore run as
independent processes with no coordinator.

## Goals

- Two working integrations: **calendar countdown** and **CI status**.
- Shared device client so API handling lives in one place.
- Public-repo safe by construction: no secrets in code, config, or history.
- Each integration testable without hardware (`--once`, `--dry-run`).
- CI status must not consume GitHub GraphQL quota (operator constraint) and must
  minimize REST quota via conditional requests.

## Non-goals (v1)

- Hub/daemon display scheduler (firmware priority arbitration makes it redundant).
- Cloud-relay operation; everything targets the local API.
- Non-macOS calendar sources (Google API, ICS) — possible future integrations.

## Repo layout

```
busybar-integrations/
├── README.md                    # overview, quick start, integration index
├── LICENSE                      # existing
├── pyproject.toml               # uv-managed; defines the `busybar` package
├── src/busybar/
│   ├── __init__.py
│   ├── client.py                # BusyBarClient
│   └── config.py                # config.toml loader; BUSYBAR_HOST env override
├── integrations/
│   ├── calendar_countdown/
│   │   ├── main.py
│   │   ├── README.md
│   │   └── com.busybar.calendar-countdown.plist
│   └── ci_status/
│       ├── main.py
│       ├── README.md
│       └── com.busybar.ci-status.plist
├── config.example.toml          # committed; placeholders only
├── .gitignore                   # config.toml, .venv, __pycache__, .pytest_cache
├── .github/workflows/ci.yml    # pytest + gitleaks secret scan
└── tests/
```

## Shared client (`src/busybar/`)

`BusyBarClient(host)` wraps the device API with the calls v1 needs:

- `draw(application_name, elements, priority)` → POST `/api/display/draw`;
  returns a four-state result (drawn / rejected-409 / http-error /
  device-unreachable). *(Amended 2026-08-03 by operator ruling during
  implementation review: HTTP errors from a live device are distinguished
  from unreachable; only unreachable triggers loop backoff.)*
- `clear(application_name)` → DELETE `/api/display/draw?application_name=…`.
- `set_busy(...)` / `get_busy()` → `/api/busy/snapshot`.
- `status()` → GET `/api/status` (used for reachability checks).

Behavior rules:

- Device unreachable (USB unplugged, asleep): never raise to the caller loop —
  return unreachable, caller backs off exponentially (base 5 s, cap 5 min).
- HTTP 409 (priority below active app): expected, non-error, skipped silently.
- Timeouts short (3 s connect / 5 s read) — it is a LAN device.

Config resolution: `config.toml` beside the repo root (git-ignored), overridable
by `BUSYBAR_HOST`. Default host `10.0.4.20`.

## Integration 1 — calendar countdown (`integrations/calendar_countdown/`)

- Reads macOS EventKit via `pyobjc-framework-EventKit`. No credentials; macOS
  prompts for calendar permission on first run. Denied permission → actionable
  error message and non-zero exit.
- Poll loop (default 60 s): select the next upcoming event within
  `lookahead_hours` (default 12) from `calendars` (default: all), skipping
  all-day events by default (`include_all_day = false`).
- Display: `HH:MM · <title> in <N>m` as scrolling medium-font text,
  `application_name = "calendar_countdown"`, priority 20 (firmware scale is
  1–100: built-in apps sit at 10, an active BUSY session at 90 — 20 is ambient:
  above idle built-ins, below everything that matters).
  Within `warn_minutes` (default 5) of start, re-draw in warning color.
- `auto_busy = false` (default): when true, sets the BUSY timer for the duration
  of the active event and clears it after.
- No event in window → clear own elements.

## Integration 2 — CI status (`integrations/ci_status/`)

- **REST only, by design.** Uses `gh api` with explicit REST paths so the stored
  `gh` CLI auth is reused but GraphQL is never touched (the convenience
  subcommands `gh pr status` / `gh pr checks` / `gh run list` are GraphQL-backed
  and are prohibited in this codebase). GitHub REST and GraphQL quotas are
  separate buckets; this integration draws only on REST.
- Endpoints per configured repo (poll default 120 s):
  - `GET /repos/{owner}/{repo}/actions/runs?per_page=5` — recent runs.
- Every request is conditional: cached `ETag` sent as `If-None-Match`; a
  `304 Not Modified` does not count against the primary REST rate limit, so
  steady-state quota use is near zero.
- Runner coverage: workflow runs are reported identically whether jobs execute
  on GitHub-hosted or self-hosted (e.g., Portable-GHAR ephemeral) runners — both
  are tracked with no extra configuration.
- Display behavior:
  - All green → draw nothing (default) or a small quiet glyph
    (`show_green = false`).
  - Any run with `conclusion` ∈ {failure, timed_out, startup_failure} → red
    alert, `application_name = "ci_status"`, priority 60: preempts the calendar
    (20) via firmware arbitration but defers to an active BUSY work session
    (90) — during a session the alert instead blinks the status LED red via
    `led_notification_color`. Cleared when the offending workflow goes green.
  - `stale_queued_minutes` (default: off) → a run in `status = queued` longer
    than the threshold draws a yellow "CI stuck" alert — catches an offline
    self-hosted runner, which GitHub-hosted runs essentially never hit.
- `gh` missing or unauthenticated → actionable error message and non-zero exit.

## Configuration (`config.example.toml`)

```toml
[device]
host = "10.0.4.20"          # USB-Ethernet default; set LAN IP for Wi-Fi

[calendar_countdown]
poll_seconds = 60
lookahead_hours = 12
warn_minutes = 5
include_all_day = false
auto_busy = false
# calendars = ["Work"]      # default: all calendars

[ci_status]
poll_seconds = 120
repos = ["your-user/your-repo"]
show_green = false
# stale_queued_minutes = 15
```

Real `config.toml` is git-ignored. There are no secrets anywhere in the system:
EventKit is local, `gh` keeps its own credentials, the device API is
unauthenticated on the LAN.

## Public-repo hygiene

- The repo is **already public** — treat every commit as public from now on.
- `config.example.toml` uses placeholders only; personal repo names, LAN IPs
  other than the documented USB default, and calendar names never get committed.
- CI runs pytest and a gitleaks secret scan on every push/PR.
- README follows the structure of the operator's other public repos
  (purpose → requirements → setup → configuration reference → integration index).

## Error handling summary

| Condition | Behavior |
|---|---|
| Device unreachable | Exponential backoff (5 s → 5 min), keep running |
| Draw returns 409 | Expected (higher-priority app active); skip silently |
| Calendar permission denied | Clear error + exit non-zero |
| `gh` unauthenticated/missing | Clear error + exit non-zero |
| GitHub 5xx / network error | Treat as no-change; retry next poll |

## Testing

- Pure logic is unit-tested with pytest: next-event selection, countdown
  formatting, run→display-state mapping, ETag cache behavior (mocked `gh`).
- `--once` runs a single poll cycle; `--dry-run` prints the draw payload (and,
  for ci_status, the REST endpoints it would hit) instead of POSTing.
- Live smoke test = `--once` against the real device.

## Runtime

Each integration ships a launchd user LaunchAgent plist (`RunAtLoad`,
`KeepAlive`) with a documented `launchctl bootstrap` install one-liner; both also
run fine manually via `uv run`.
