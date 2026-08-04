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

## 2026-08-03 — v1.1 display redesign

**Status:** Implemented (branch `dev/claude/display-v1.1`).

Replaces the single scrolling text line with layout **"A + progress accent
bar"**: a 2px-wide vertical bar (progress-to-event), a top title row
(time + event title), and a native `countdown` element that ticks on-device
every second instead of being redrawn on each poll.

### Calendar countdown (`integrations/calendar_countdown/`)

`logic.build_elements(event, now, cfg, timeout_s, in_progress)` replaces the
old `build_elements(text, warning, timeout_s)` + `format_countdown`. Element
list, by id:

- **`bar`** — `rectangle`, `x=0`, `width=2`, `fill=solid`, anchored to the
  bottom (`y = 16 - height`) so it drains downward as the event approaches.
  Height is `round(16 * minutes_left / progress_window_minutes)` (new config
  key, default 60), clamped to `[1, 16]`; full height once `minutes_left >=
  window`. For an in-progress event the bar is always full-height and teal.
- **`time`** — `tiny` font, gray `#B4B2A9FF`, `HH:MM` of the event start.
  Present only for an *upcoming* event; omitted for an in-progress one (its
  start time is no longer the relevant number — the countdown below already
  targets the end).
- **`title`** — `small` font, always white regardless of urgency, `ascii_safe`
  event title. `x` is `26` (right of the time label) for upcoming events or
  `4` (flush left) when in progress. Scrolls (`scroll_rate=2000`, matching the
  v1 delays) only if the title is estimated not to fit the remaining width;
  otherwise static. Fit is estimated at 5px/char for the `small` bitmap font —
  a conservative constant (`SMALL_FONT_CHAR_PX` in `logic.py`), not a firmware
  metrics query, so it may occasionally scroll a title that would have just
  fit.
- **`countdown`** — native `countdown` element (`direction=time_left`,
  `show_hours=when_non_zero`, `timestamp` as the required **string** of unix
  seconds). Targets `event.start` for an upcoming event or `event.end` while
  in progress. This is what makes the display tick every second without a
  redraw from the integration.

Urgency color (`bar` + `countdown`, **not** `title`, which stays white):
white by default, amber `#EF9F27FF` at or below `notice_minutes` (new config
key, default 15), red `#E24B4AFF` at or below `warn_minutes` (existing,
default 5). An in-progress event overrides all of this: full teal
`#1D9E75FF` bar, countdown to `event.end` in teal.

`main.run_once` now calls `select_active_event` unconditionally (previously
only under `auto_busy`) and prefers an in-progress event over the next
upcoming one for display purposes — showing "this meeting ends in 12m" is
more useful than "next meeting starts in 3h" while one is already running.
`auto_busy` behavior is unchanged. `format_countdown` was removed (its only
caller, the old single-line renderer, is gone, and no test needed it either).

### CI status (`integrations/ci_status/`)

`logic.build_ci_payload` gains a full-panel background badge behind the
scrolling text for the two alert states: `rectangle` `x=0 y=0 w=72 h=16
radius=2 fill=solid`, red `#A32D2DFF` for failures / amber `#BA7517FF` for
stuck, under bold-font text (white `#FFFFFFFF` on red, black `#0B0B0BFF` on
amber). `show_green` behavior and LED blink behavior are unchanged (plain
text, no badge, no LED noise). Failure still wins over stuck when both are
present (unchanged precedence).

### Firmware gotcha found during on-device verification

`RectangleElement` defaults to `border_width=1` with `border_color=#FFFFFFFF`
per the OpenAPI spec. For the 2px-wide progress bar this default border alone
covers the entire element, rendering it solid white regardless of the
requested `fill_colors` — confirmed by capturing raw frames via
`GET /api/screen?display=0` (base64 **BGR** pixel data, not RGB — a second,
tooling-side gotcha in the capture script, not the firmware) and reading back
actual pixel values. Both the bar and the CI badge background now set
`"border_width": 0` explicitly; the bar bug was invisible in the request
payload (looked correct) and only showed up rendered.

### Config (`src/busybar/config.py`, `config.example.toml`)

Added to `[calendar_countdown]`: `notice_minutes = 15`,
`progress_window_minutes = 60`.

## 2026-08-03 — v1.3 "Color Horizon" display redesign

**Status:** Implemented (branch `dev/claude/display-v1.3.1`). **Corrected
2026-08-03** after the first pass's production render came out mashed --
see "Correction: font ink offset + native countdown replaced with text"
below. The element table, state palette, and firmware-findings subsections
in this entry describe the corrected (current) design; the original pass's
`align="top_right"` native-countdown approach was live only briefly and is
superseded.

Replaces v1.1's "progress accent bar + title row" with a denser, full-panel
card layout: a gradient background that itself signals urgency, a
horizontal drain track under the title, and a two-card bottom row (a large
digital-clock-style start time, or an "ENDS" label, alongside a large
countdown). Config keys, `logic.build_elements` signature, and
`main.run_once`'s public contract (dry-run behavior, summary strings ending
in the `DrawResult` value, priority 20, `timeout = 1.5 * poll_seconds`) are
all unchanged from v1.1.

### Design provenance

Layout structure (the two-card bottom row, the divider, the horizontal
drain track replacing the vertical bar) originated from a Codex structural
pass. Gemini reviewed that structure for contrast and legibility against
the panel's small bitmap fonts and flagged the need for state-differentiated
background gradients (rather than a fixed dark background) so urgency reads
at a glance even before the title or countdown color registers, plus
brighter title/digit colors per state to hold contrast against each
gradient. Claude synthesized the final four-state threshold model
(`normal` / `notice` / `warning` / `in_progress`, reusing the existing
`notice_minutes` / `warn_minutes` config semantics from v1.1's three-color
scheme rather than introducing new keys) and the concrete hex palette below.

### Element table (`integrations/calendar_countdown/logic.build_elements`)

Draw order is z-order, first = behind. `time_card`+`time` and `ends` are
mutually exclusive (upcoming vs. in-progress); every other element is
always drawn.

| id | type | geometry | notes |
|---|---|---|---|
| `bg` | rectangle | x0 y0 72×16 | `fill=gradient_v`, per-state colors, `border_width=0` |
| `title` | text | x1 **y=-2** w70, font `small` | `ascii_safe(title).upper()`, per-state color; scrolls (`scroll_rate=2000`, 800ms start/repeat delay) only if it doesn't fit; ink lands rows 0-4 (see ink-offset table below) |
| `track` | rectangle | x0 y5 72×1 | fixed `#24193BFF` groove, solid, `border_width=0` |
| `track_fill` | rectangle | x0 y5 w×1 | `w = round(72 * minutes_left / progress_window_minutes)` clamped `[1,72]`; `gradient_h` per-state (in-progress: solid `#24D6C5FF`, always full width -- no drain) |
| `time_card` (upcoming only) | rectangle | x0 y6 34×10 r1 | fixed `#062238FF` solid |
| `time` (upcoming only) | text | x1 **y=4**, font `extra_large` | fixed `#8DDEFFFF`, local `HH:MM` of event start; ink lands rows 6-15, exactly filling `time_card` |
| `ends` (in-progress only) | text | x3 **y=6**, font `bold` | fixed `#8CFFF4FF`, literal `"ENDS"`, no card behind it; ink lands rows 8-14 |
| `divider` | rectangle | x34 y6 2×10 | per-state solid, `border_width=0` |
| `cd_card` | rectangle | x36 y6 36×10 r1 | per-state solid, `border_width=0` |
| `cd_text` | text | **x38 y4**, font `extra_large` | per-state digit color; text from `_format_countdown(minutes_left)` (see below); ink lands rows 6-15, matching `cd_card`; no `align` field |

### State palette

| State | Threshold | `bg` gradient | `title` | `track_fill` gradient | `divider` | `cd_card` | `cd_text` digits |
|---|---|---|---|---|---|---|---|
| `normal` | `minutes_left > notice_minutes` | `#160A2EFF` → `#03040DFF` | `#FFD166FF` | `#1ED6FFFF` → `#5CFFB1FF` | `#643B8FFF` | `#062A22FF` | `#6BFFD0FF` |
| `notice` | `minutes_left <= notice_minutes` | `#291300FF` → `#070301FF` | `#FFE3A3FF` | `#FF9F1CFF` → `#FFE66DFF` | `#C37A0CFF` | `#3A2000FF` | `#FFC247FF` |
| `warning` | `minutes_left <= warn_minutes` | `#30040BFF` → `#080103FF` | `#FFE7ECFF` | `#FF204EFF` → `#FF7A22FF` | `#E02A4CFF` | `#3A0711FF` | `#FF4B68FF` |
| `in_progress` | event currently active (overrides the above) | `#032B2CFF` → `#010809FF` | `#83FFF3FF` | solid `#24D6C5FF`, full width | `#178C88FF` | `#063238FF` | `#64FFEAFF` |

`warning` and `notice` thresholds are inclusive at their boundary
(`minutes_left <= warn_minutes` wins over `<= notice_minutes` when both are
true), matching v1.1's `_urgency_color` boundary semantics. `in_progress` is
orthogonal to the other three -- it overrides threshold evaluation entirely
rather than being reached through it.

### `cd_text` countdown format (`_format_countdown`)

Minutes-granular, floored (not rounded): `<M>m` under 60 minutes (e.g.
`"54m"`), `<H>h<MM>m` at/above 1 hour (e.g. `"1h05m"`, minutes zero-padded),
and `<H>h` only (minutes dropped) at 10+ hours (e.g. `"12h"`). The 10-hour
cutover is required, not cosmetic: the full `<H>h<MM>m` form at 2-digit
hours is 6 glyphs (e.g. `"12h00m"`), measured on-device at ~37px in the
`extra_large` font -- 1px wider than the ~34px available between `cd_text`'s
x and the panel's right edge, so it would clip. Below 10 hours the full form
is at most 5 glyphs (`"9h59m"` ≈ 31px), which fits comfortably.

### Firmware findings from on-device verification

- **`RectangleElement`'s default 1px white border** (same v1.1 gotcha)
  applies to every rectangle in this layout -- `bg`, `track`, `track_fill`,
  `time_card`, `divider`, and `cd_card` all set `border_width=0` explicitly.
- **The draw endpoint upserts elements by id within an `application_name`;
  it does not replace that app's whole element set.** Found by drawing the
  upcoming layout (ids include `time_card`+`time`), then drawing the
  in-progress layout (ids include `ends` instead) without an explicit clear
  in between: the previous draw's opaque `time_card` and `time` digits
  remained rendered, visible behind the new `ends` text, until their own
  `timeout` expired. `clearDisplay` (`DELETE
  /api/display/draw?application_name=...`) does fully remove an app's
  elements -- confirmed by clearing and observing the panel fall through to
  whatever lower-priority app was already drawing underneath. Fixed in
  `main.run_once` via an optional `state` dict that remembers the previous
  `in_progress` value across polls and calls `client.clear(APP)` only at the
  upcoming ⟷ in-progress transition (not on every poll, which would flicker
  the display). **Corroborated by a second, narrower instance of the same
  upsert quirk found during the v1.3.1 correction pass**: redrawing
  `track_fill` under the *same* id but with a shorter `fill_colors` array
  (2-color `gradient_h` → 1-color `solid`, exactly the shape change at the
  upcoming ⟷ in-progress boundary) without a clear left the second gradient
  stop's color bleeding through at the old array index, even though `fill`
  itself and `fill_colors[0]` updated correctly. A fresh `clear()` before
  the draw eliminated it. The existing `state`-gated transition clear
  already covers this case (it fires at exactly this boundary), so no
  additional code change was needed -- confirmed by reproducing the artifact
  with a raw sequential draw (no clear) and then confirming it disappears
  with a clear inserted, matching what `main.run_once` actually does in
  production.

### Correction: font ink offset + native countdown replaced with text

The first v1.3 pass shipped and immediately mashed the live render: the
title collided with the drain track, and `ends` clipped at the panel's
bottom edge. Root-caused via fresh live-frame captures and fresh-id
firmware probes:

1. **Every font renders its ink ~2px below the element's `y`.** Confirmed
   uniformly across the `small`, `extra_large`, and `bold` fonts used in
   this layout (also independently corroborated by the firmware's own font
   registry: `small`→`FONT_BUSY_REGULAR_5` (5px), `extra_large`→
   `FONT_BUSY_BOLD_10` (10px bold), `bold`→`FONT_BUSY_BOLD_7` (7px) --
   ink-row heights measured on-device match these pixel heights exactly).
   The `y` values in the element table above are already offset-corrected
   so ink lands where the geometry implies:

   | Element | Font | Requested `y` | Ink rows | Height |
   |---|---|---|---|---|
   | `title` | `small` (5px) | `-2` | 0-4 | 5px |
   | `time` / `cd_text` | `extra_large` (10px bold) | `4` | 6-15 | 10px |
   | `ends` | `bold` (7px) | `6` | 8-14 | 7px |

2. **The native `countdown` element's digits render only 5px tall**, in
   both `MM:SS` and `H:MM:SS` modes -- confirmed with fresh-id on-device
   probes. The original pass's "10px" figure was a stale controller
   measurement corrupted by the firmware's id-reuse-type-change quirk (an
   id whose element `type` changes between draws, `countdown`→something
   else, can serve stale pixel data from the previous type). Since large
   numerals are the operator's core requirement, the native `countdown`
   element is unusable here regardless of the ink-offset fix. Replaced with
   a plain `text` element (`cd_text`, `extra_large` font, 10px) formatted
   each poll by `_format_countdown` (above) -- this also removes the
   `align="top_right"` right-anchoring used in the first pass entirely
   (`align` re-anchors screen-relative, not card-relative, and was
   implicated in the mash), in favor of a fixed left position (`x=38`)
   within `cd_card`.
3. **Title is now rendered uppercase** (`.upper()` after `ascii_safe`),
   which kills descenders (g, y, p, j, q) -- descenders are exactly what
   pushed the title's ink into the track row before the offset fix, so
   uppercasing gives extra headroom against a repeat.
4. **id hygiene:** the countdown element was renamed from `countdown` to
   `cd_text` specifically because its `type` changed (`countdown`→`text`);
   reusing the old id across a type change is the same quirk that corrupted
   the "10px" measurement in finding 2. `main()`'s startup `client.clear(APP)`
   additionally protects a process restart against any lingering
   `countdown`-typed element left by an older deploy.

**Verification for this correction round** used a programmatic overlap
check (not just visual inspection) against live frame captures for all four
states, with the title `"Gym pyjama day"` (uppercases to `"GYM PYJAMA DAY"`,
deliberately chosen for descenders + length): for each state, scan the
captured frame for pixels matching each text element's exact color and
assert the rows where that color appears fall entirely within the
element's expected row band (`title` ⊆ rows 0-4, `time`/`ends`/`cd_text` ⊆
rows 6-15, no element's ink row set includes row 5, the track). See the
implementation report for the verbatim check output and captures.
