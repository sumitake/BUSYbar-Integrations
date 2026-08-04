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

## 2026-08-03 — v1.4 "airy" display refinement

**Status:** Implemented (branch `dev/claude/display-v1.4`).

Operator-approved refinement on top of the v1.3.1 correction: more
breathing room, card surfaces removed entirely, both numerals (`time` and
`cd_text`) drop from `extra_large` (10px bold) to `large` (9px) so they
stay equal-sized. `logic.build_elements`'s signature and `main.run_once`'s
public contract (dry-run behavior, summary strings ending in the
`DrawResult` value, priority 20, `timeout = 1.5 * poll_seconds`) are
unchanged. Config keys are unchanged.

### Design principles (operator-set)

These govern this and future display iterations, not just v1.4:

- **Card/panel surfaces are acceptable ONLY with strong luminance contrast**
  against both the ambient background and their own text: either a
  near-black surface with bright text, or an inverse chip (bright saturated
  surface with near-black text, as the `ci_status` failure badge does).
  Mid-luminance dim fills -- the v1.3.1 cards (`#062238`, `#062A22`, etc.)
  -- read as glow-mud on emissive LEDs and are prohibited. This is why v1.4
  removes card surfaces entirely rather than re-tuning their luminance.
- **Numerals track the countdown's size: both change together, never
  independently.** `time` and `cd_text` always use the same font (`large`
  in v1.4); a future size change must move both, not one.
- **Any geometry change must pass the frame-capture ink-overlap + buffer
  gate before merge** -- the same programmatic verification methodology
  used for this section's verification (below), not a visual glance at a
  single static frame. This directive is itself the direct product of the
  v1.3 mash: a cursory visual check missed both root causes there.

### Row budget (where the freed rows went)

v1.3.1 (with cards) used all 16 rows edge-to-edge with no deliberate gaps:
title ink 0-4, track 5, cards+content 6-15. v1.4 removes the cards and
opens up a genuine blank buffer row, trading density for legibility:

| Rows | v1.3.1 | v1.4 |
|---|---|---|
| 0-4 | `title` ink (small, y=0) | `title` ink (small, y=-2) -- same height, shifted up 2px (top-flush) |
| 5 | `track`/`track_fill` ink (y=5) | **blank buffer** -- deliberate, asserted in tests and on-device |
| 6 | (inside `time_card`/`cd_card`, y=6) | `track`/`track_fill` ink (moved down to y=6, now edge-to-edge -- the "horizon line") |
| 7 | (inside cards) | **blank buffer** -- 1px gap between the track and the numerals below |
| 8-14 | `time`/`cd_text` ink (extra_large, inside cards) / `ends` ink (bold) | `ends` ink (bold, y=6) -- unchanged position from v1.3.1, still coexists numerically with the track's `y` but not its ink (offset places it 2 rows below) |
| 15 | (inside cards, bottom-flush) | `time`/`cd_text` ink continues to here (large, y=5) -- bottom-flush, deliberate |

Net effect: the panel now has two genuine 1px negative-space rows (5 and
7) it didn't have before, at the cost of ~1px of numeral height (9px vs
10px bold) -- the explicit "more breathing room" trade the operator asked
for.

### Element table (`integrations/calendar_countdown/logic.build_elements`)

No card elements in v1.4. `time` and `ends` are mutually exclusive
(upcoming vs. in-progress); everything else is always drawn.

| id | type | geometry | notes |
|---|---|---|---|
| `bg` | rectangle | x0 y0 72×16 | unchanged from v1.3.1: `fill=gradient_v`, per-state colors, `border_width=0` |
| `title` | text | x2 y-2 w68, font `small` | uppercased, per-state color, ink rows 0-4; 2px side margins (was x1 w70) |
| `track` | rectangle | x0 y6 72×1 | fixed `#24193BFF`, solid, edge-to-edge, `border_width=0` (was y5) |
| `track_fill` | rectangle | x0 y6 w×1 | same drain math and per-state fills as v1.3.1, just moved to y6 |
| `time` (upcoming only) | text | x2 y5, font `large` | fixed `#8DDEFFFF`, local `HH:MM`, ink rows 7-15, floats on `bg` (no card) |
| `ends` (in-progress only) | text | x2 y6, font `bold` | fixed `#8CFFF4FF`, literal `"ENDS"`, ink rows 8-14 (position unchanged from v1.3.1; only x moved 3→2) |
| `divider` | rectangle | x34 y8 2×7 | per-state solid, ink rows 8-14, `border_width=0` (was y6 h10) |
| `cd_text` | text | x39 y5, font `large` | per-state digit color; `_format_countdown(minutes_left)`; ink rows 7-15, floats on `bg` (no card); no `align` field |

### `cd_text` countdown format -- width check redesigned (important finding)

The v1.3.1 spec's `>=10h -> hour-only` cutoff, re-verified on-device for
the new `large` font, turned out **unsafe even for single-digit hours**:
this font's `'1'` glyph measures 5px advance vs 7px for every other digit
(a ~30% difference measured by on-device glyph differencing -- see
`GLYPH_ADVANCE_PX` in `logic.py`). Whether a given `"<H>h<MM>m"` string
fits the 33px budget (`CD_TEXT_MAX_WIDTH = 72 - 39`) therefore depends on
which actual digits appear, not on the hour count:

| String | Measured width | Fits 33px? |
|---|---|---|
| `"1h05m"` | 32px | yes |
| `"1h59m"` | 32px | yes (hours digit's `'1'` is enough headroom regardless of minutes) |
| `"9h11m"` | 30px | yes (two `'1'`s in the minutes rescue a non-`'1'` hours digit) |
| `"2h05m"` | 34px | **no** |
| `"5h55m"` | 34px | **no** |
| `"9h59m"` | 34px | **no** |
| `"0h00m"` | 34px | **no** (not reachable in practice -- hours=0 always uses the `"<M>m"` form) |

`_format_countdown` now computes the full form's actual width via
`GLYPH_ADVANCE_PX`/`_text_width_px` and falls back to the hour-only `"<H>h"`
form per-string, rather than on a fixed hour threshold. Two-digit hours
(10+) fail the same check on their own digits and fall through to
hour-only with no special case needed -- the v1.3.1 spec's stated behavior
("10+ hours drop minutes") holds as an emergent outcome, just via a
different, more accurate mechanism than originally designed. `time`'s own
budget was also re-checked: `"HH:MM"` (`"00:00"`–`"23:59"`) measures 29px
against a 32px budget (x2 to the divider at x34) -- comfortable margin, no
fallback needed there.

### Firmware findings from on-device verification

- Ink-offset model (established in the v1.3.1 correction) holds for the
  `large` font too: requested `y` + 2px = actual ink start row, confirmed
  by direct glyph-height measurement (`"1h05m"` at `y=0` measured ink rows
  2-10, i.e. 9px tall starting 2 rows below the requested `y` -- matches
  `large`/`FONT_BUSY_REGULAR_9`'s known 9px height exactly).
  `RectangleElement`s (the `track` and `divider`) have no such offset.
- The `large` font is genuinely not fixed-width (see the width-check
  section above) -- this project's earlier font-width constants
  (`SMALL_FONT_CHAR_PX`, the retired `CD_TEXT_CHAR_PX`) were flat
  per-character averages that happened to work well enough for their
  purposes (a scroll/no-scroll decision has slack; a hard pixel budget for
  a right-flush numeral does not). `GLYPH_ADVANCE_PX` is the first
  per-glyph (not per-font-average) width table in this codebase.

**Verification** used the same programmatic ink-overlap methodology as the
v1.3.1 correction, extended with buffer- and margin-specific checks per
this round's brief: row 5 has no foreground ink (bg-gradient only);
columns 0-1 carry no `title`/`time`/`ends`/`cd_text`/`divider` ink (only
the full-width `bg`/`track`/`track_fill` legitimately span there); the
divider has at least one blank column on each side (checked directly:
neither the neighboring text's ink nor the divider's own ink reaches the
immediately adjacent column). See the implementation report for the
verbatim check output and captures.

## 2026-08-03 — v1.5 running-CI badge

**Status:** Implemented (branch `dev/claude/ci-running-v1.5`); revised
in-branch (operator decision) to generalize the priority/dwell mechanics
into a shared framework, tune the alternation rhythm, and add a
GitHub-API-quota overlay -- see "Framework generalization, tuned
alternation, and quota frames" below, added after the original probe
findings and feature description but describing the code's final state.

New feature: `ci_status` shows an actively-running CI job on the bar,
alternating with the calendar. The design brief assumed a "clean
alternation, zero coordination" mechanism based on the OpenAPI doc's
priority-arbitration description. **Two empirical probes done before any
implementation found the doc's description doesn't match firmware 1.1.1's
actual behavior**, which reshaped the whole approach -- see below before
the feature description, since it changes what "alternating" actually
means here.

### Probe findings (read this first -- the design depends on it)

**Finding 1: equal priority from a different `application_name` is
REJECTED, not an override.** The OpenAPI doc states: *"A draw request is
accepted when its priority is >= the priority of the currently running
system app. Equal-priority requests from a different application_name
override whatever is on screen."* Probed directly: with the live
`calendar_countdown` agent showing at priority 20, a draw from a different
`application_name` at priority 20 returned `409 {"error":"Not drawn due to
low priority"}` in every trial (19, 20 both rejected); only priority 21
(strictly greater) succeeded. **Consequence: the running badge cannot use
priority 20 as the brief specified -- it must use priority 21
(`RUNNING_PRIORITY` in `ci_status/logic.py`), a deliberate, evidence-backed
deviation from the literal brief.**

**Finding 2: occluded elements are EVICTED, not restored.** Probed with
two test apps (`probe_a` at priority 21, long timeout; `probe_b` at
priority 22, occluding it) across two scenarios -- (a) `probe_b`'s own
timeout expiring, (b) `probe_b` being explicitly cleared. In **both**
cases, `probe_a`'s still-live element (well within its own 30s timeout)
did **not** reappear; the panel went black instead, and stayed black until
a fresh draw from any app. A follow-up probe confirmed the "currently
running app" baseline resets low enough after eviction that a plain
priority-20 draw from a third app then succeeds again. **Consequence: the
brief's "if occluded elements restore: clean alternation, zero
coordination" branch does not apply. This forces the "if evicted" fallback
design:** the badge occupies the screen for its own timeout, then the
panel goes blank until *some* app performs a fresh draw -- either the
badge's own next cycle, or the calendar's independent 60s redraw landing
in the gap by chance (no cross-process coordination exists, and none was
added).

**Observed rhythm (on-device, ~130s window against the live
`calendar_countdown` agent, badge at priority 21/10s timeout/20s cadence,
sampled every 2s):**

```
sample counts: {'BADGE': 33, 'BLANK': 31}
```

The rhythm is **exactly** badge-visible-~10s / blank-~10s, repeating every
~20s cycle -- and the live calendar agent did **not** reclaim the screen
even once across six ~10s gaps spanning more than two of its own 60s
redraw cycles. This is a materially different experience than "badge
alternates with calendar": in practice, while a run is active, the panel
shows the badge roughly half the time and is **dark** the other half; the
calendar effectively does not get airtime back until the CI run finishes
(cadence reverts to `poll_seconds`) or the calendar's redraw happens to
land in a gap by chance, which was not observed in this test window. This
is exactly the kind of deviation the brief asked to be reported
prominently rather than silently redesigned around -- implemented as
specified (fixed ~10s badge timeout, `running_poll_seconds` cadence), not
adjusted to hide the gap, so the operator can judge whether the parameters
need tuning.

### Feature: running-CI badge (`integrations/ci_status/`)

Per configured repo, `GET .../actions/runs?status=in_progress&per_page=5`
(separate ETag slot from the existing failure/stuck poll -- different URL,
`RestPoller._running_etags` not `._etags`). Across all repos, the
most-recently-started `in_progress` run is selected (`select_running_run`);
if others are also running, a `+N` suffix is added to the title.

**Title ribbon** (`small` font, `y=-2`, uppercase, scrolls if it doesn't
fit): `REPO #PR WORKFLOW` (PR number from `run.pull_requests[0].number`;
fork/push runs with an empty `pull_requests` array fall back to
`head_branch`), with a `+N` suffix when other runs are active.

**Numeral row** (`large` font, `y=5`): ETA remaining, reusing
`calendar_countdown.logic._format_countdown` verbatim (imported across
integrations, along with `ascii_safe`, `_title_fits`,
`SCROLL_RATE`/`SCROLL_DELAY_MS`, `PANEL_WIDTH`/`PANEL_HEIGHT` -- see the
comment at the top of `ci_status/logic.py` for why these specific helpers
are shared while the layout geometry/palette constants are independently
declared per integration).

- ETA = median duration of the last 5 successful runs of the *same*
  workflow (`.../workflows/{workflow_id}/runs?status=success&per_page=5`,
  duration = `updated_at - run_started_at`, cached per `workflow_id` for
  the process's lifetime in `RestPoller._eta_cache` -- a successful fetch
  is cached even if the history is confirmed empty; a network/HTTP error is
  **not** cached, so the next encounter retries rather than permanently
  locking the workflow into "no history") minus elapsed (`now -
  run_started_at`), floored at 0.
- History exists: `"~" + _format_countdown(eta)` (e.g. `"~4m"`,
  `"~1h05m"`), or `"soon"` once the estimate floors to under a minute
  (covers both "past the median" and "a few seconds under a minute left" --
  neither reads sensibly as `"~0m"`).
- No history: `_format_countdown(elapsed) + " in"` (e.g. `"3m in"`).

**Track row**: repurposed from the calendar's drain metaphor to a
fill-*up* progress bar: `elapsed / median`, clamped to `[1, PANEL_WIDTH]`,
full width when the median is unknown (same defensive shape as
`_track_fill_width`/`_progress_width`). Solid cyan fill (`RUNNING_TRACK_FILL_COLOR`,
`#29B6F6FF`) -- no gradient, per the brief.

### Palette and geometry

A distinct cyan/blue theme, following the same luminance-contrast
principle established in v1.4 (near-black gradient background + bright
saturated text; no mid-luminance "dim-mud" fills):

| Element | Color | Notes |
|---|---|---|
| `bg` | `#031A2EFF` → `#00060DFF` gradient | deep blue night, distinct hue from every calendar state |
| `title` | `#7FDBFFFF` | bright sky-cyan |
| `track` (groove) | `#0F2A42FF` | decorative, not text-bearing -- same treatment as the calendar's `TRACK_COLOR` |
| `track_fill` | `#29B6F6FF` | solid, saturated cyan (spec: "solid cyan") |
| `eta` (numeral) | `#66E1FFFF` | bright cyan, distinguishable from the title for a small hierarchy |

Geometry follows the v1.4 "airy" row template (title ribbon / blank buffer
row 5 / full-width horizon-line track at row 6 / numeral row) but is
independently declared in `ci_status/logic.py` (`RUNNING_TITLE_X`,
`RUNNING_TRACK_Y`, etc.) rather than importing the calendar's geometry
constants -- only the *algorithms* are shared across integrations, not the
layout objects, since the two badges are visually similar but structurally
distinct (no divider, no card, a single numeral).

### Cadence and precedence

`ci_status` shortens its poll interval to `running_poll_seconds` (new
config key, default 20) while any configured repo has an `in_progress`
run, reverting to `poll_seconds` when idle (`main.next_poll_seconds`, a
pure function of the post-poll `running_cache` so it's unit-testable
without mocking `time.sleep`). The running badge's own element `timeout`
is a fixed `RUNNING_BADGE_TIMEOUT_S = 10` regardless of
`running_poll_seconds`'s configured value -- per the brief ("timeout
~10s"), not derived from the cadence. A much larger or smaller
`running_poll_seconds` than the default would change the observed ratio of
badge-visible to dark time; this is a real configuration interaction, not
a bug, and is called out in the README.

Precedence in `build_ci_payload`: **failure (60) > stuck (60) > running
(21) > quiet green (60) > nothing**. Failure and stuck are evaluated first
specifically so an active alert always wins over "just" a running-job
status update, even if both conditions are true in the same poll.

### Config additions (`src/busybar/config.py`, `config.example.toml`)

Added to `[ci_status]`: `show_running = true`, `running_poll_seconds = 20`.

### Verification

On-device: the two probes above; captured frames of all four badge content
variants (PR number, branch fallback, `"soon"`, `"3m in"`) passing the
same ink-overlap + buffer gate used for the v1.4 calendar work (title ⊆
rows 0-4, `eta` ⊆ rows 7-15, row 5 has no foreground ink, columns 0-1 have
no text ink); the ~130s alternation-rhythm observation above. See the
implementation report for verbatim check output, the probe transcripts,
and captures.

### Framework generalization, tuned alternation, and quota frames

Operator decision after reviewing the probe findings above: (1) generalize
the priority/dwell mechanics discovered here into a reusable framework in
the shared package, so future integrations adopt the same pattern instead
of re-deriving it; (2) tune the alternation rhythm toward "near-true"
rather than shipping the ~50%-dark finding as-is; (3) add a rotating
GitHub-API-quota overlay (GraphQL + REST buckets) that joins the running
badge in the same dwell/gap rotation while a run is active. All three
landed together on this branch; this subsection documents the final
design, generalizing the language above (`RUNNING_PRIORITY`, the
running-badge-specific rhythm numbers) into the shared vocabulary below.

#### Display tier framework (`src/busybar/display.py`)

The two probe findings above are not specific to the running-CI badge --
any integration that wants to time-share the screen with another
integration runs into the same firmware behavior. Rather than let each
integration re-derive its own priority number and dwell logic, the ladder
and the shared contracts now live in `src/busybar/display.py`:

| Tier | Constant | Priority | Contract |
|---|---|---|---|
| Ambient | `PRIORITY_AMBIENT` | 20 | Persistent baseline apps (e.g. the calendar). Redraw at least every `AMBIENT_REDRAW_SECONDS` (10s); element `timeout` = `ambient_timeout(poll_seconds)` (1.5x poll, floored). Must tolerate eviction -- the contract does not promise the screen back, only that redrawing often enough gives it a fair chance to reclaim gaps. |
| Overlay | `PRIORITY_OVERLAY` | 21 | Short-dwell, time-shared frames (e.g. the running badge, quota frames). Draw with `timeout` = `OVERLAY_DWELL_SECONDS` (10s), then stay silent for >= one more dwell period before redrawing (`overlay_gap_elapsed(last_dwell_end, now) >= OVERLAY_DWELL_SECONDS`) -- this is what lets the ambient tier's own redraws land in the gap instead of the overlay tier hogging every cycle. |
| Alert | `PRIORITY_ALERT` | 60 | Urgent, preempting states (CI failure/stuck, calendar BUSY-adjacent alerts). Always strictly above the overlay tier so it preempts unconditionally. |
| Session | `PRIORITY_SESSION` | 90 | Reference only -- the firmware's own BUSY/CUSTOM tier, not something an integration draws at directly. |

Every tier boundary is a **strictly greater** priority than the one below
it, never equal -- Finding 1 above (equal priority from a different
`application_name` is rejected, not an override) makes "greater-or-equal"
adjacency actively wrong, not just imprecise. `test_display.py` asserts
the ladder is strictly increasing and has no duplicate values, specifically
to catch a future addition that violates this.

`ambient_timeout(poll_seconds)` and `overlay_gap_elapsed(last_dwell_end,
now)` are the only two helpers factored out; nothing more was built --
both consumers (`calendar_countdown`, `ci_status`) needed exactly this
much and no more.

**Join recipe for a future integration:**
- Persistent/background display, no urgency: draw at `PRIORITY_AMBIENT`,
  poll at whatever cadence keeps `ambient_timeout(poll)` reasonable, redraw
  at least every `AMBIENT_REDRAW_SECONDS` if you want a fair shot at
  reclaiming gaps left by any overlay-tier apps sharing the device.
- Short-lived, time-shared status that should rotate with other overlay
  content: draw at `PRIORITY_OVERLAY` with `timeout=OVERLAY_DWELL_SECONDS`,
  and gate your own redraw on `overlay_gap_elapsed(...) >=
  OVERLAY_DWELL_SECONDS` so you don't starve the ambient tier or other
  overlay-tier frames sharing the rotation.
- Urgent/preempting: draw at `PRIORITY_ALERT`, and make sure your
  precedence logic checks alert conditions before overlay/ambient ones (see
  `build_ci_payload`'s failure > stuck > overlay > quiet green > nothing
  ordering for the reference implementation).

#### Tuned alternation: calendar ambient cadence

`calendar_countdown`'s default `poll_seconds` moved from 60 to 15 and then
to **10**, on-device re-measured at each step (~130s window, 2s sampling,
against the live agent, classifying each sample as the overlay's own
BADGE ink, BLANK, or OTHER -- a genuine non-badge, non-black pixel matched
by exact hex against the calendar's own palette, e.g. `#160A2E` /
`#FFD166`):

| `poll_seconds` | Dwell cycles recovered (of 6) | Sample counts (BADGE / BLANK / OTHER) |
|---|---|---|
| 60 (pre-v1.5 baseline) | 0 | 33 / 31 / 0 |
| 15 | 2 | 34 / 26 / 5 |
| 10 (shipped default) | 4 | 35 / 20 / 10 |

10s was chosen because it exactly matches `OVERLAY_DWELL_SECONDS`, giving
the ambient tier's redraw the best mathematical chance of landing inside a
gap without the two independent timers needing any actual coordination.
This is "near-true alternation," not perfect alternation -- 2 of 6 sampled
cycles at 10s still showed no recovery at all, and recovery within a
recovered cycle happened anywhere from ~2s to ~8s into the 10s gap,
because the timers remain uncoordinated by design (no IPC was added; that
was explicitly out of scope). A confirmatory run exercising the full
3-frame overlay rotation (below) independently found the calendar
reclaiming 3 of 4 sampled gap windows at the same 10s/10s setting,
consistent with the standalone measurement.

#### Quota frames (`show_quota`)

Two additional overlay-tier frames join the running badge's rotation
while a run is active, cycling `ci_badge -> quota_gql -> quota_rest ->
repeat` (`overlay_frame_sequence`, `OVERLAY_FRAME_*` constants in
`ci_status/logic.py`) -- the running badge always leads, and is the only
frame at all when `show_quota` is off.

**Data source:** `GET https://api.github.com/rate_limit`
(`RestPoller.fetch_rate_limit`), fetched once per `running_poll_seconds`
cycle while a run is active. This endpoint is documented as **exempt from
GitHub's own rate limiting**, so polling it every cycle costs nothing
against any quota pool -- it exists specifically so clients can check
their standing without spending it. No ETag caching is attempted (unlike
`fetch_median_eta`'s process-lifetime cache) since the values change
continuously and the endpoint is free regardless. `parse_rate_limit`
extracts the `core` (REST) and `graphql` buckets; a response with only one
usable bucket still yields that one rather than discarding both, and a
completely unusable response yields `None`. main.py layers a **5-minute
staleness window** (`QUOTA_STALE_SECONDS`) on top of the raw fetch: a
single failed fetch doesn't blank the frame immediately, but sustained
failures eventually do (`build_overlay_payload` returns `None` for a
frame whose data isn't available, and the caller must skip that dwell
slot entirely -- no draw, no clear, no stale numbers).

**Frame layout** (v1.4 row template, numeral-floor rule -- every numeral
is `large` font, floored not rounded): title ribbon (`small`, uppercase)
reads `GITHUB GRAPHQL` or `GITHUB REST` -- these were originally
abbreviated `GH GRAPHQL`/`GH REST`, but the operator amended them to the
unabbreviated form; `GITHUB GRAPHQL` exceeds the 68px title ribbon width
at the `small` font and scrolls per the existing title-scroll rule
(`_title_fits`), same as any other overlay title that doesn't fit --
`GITHUB REST` is short enough to sit static. Track row: fraction of the
bucket used, `width = round(PANEL_WIDTH * used / limit)` clamped to `[1,
PANEL_WIDTH]` (the same defensive clamp shape as every other track-fill
calculation in this codebase; verified against a real GitHub account
where the GraphQL bucket's reported `used` exceeded `limit` -- an observed
live-data quirk in GitHub's point-based GraphQL cost accounting, not a
parsing bug -- and the clamp correctly rendered a full track rather than
overflowing or crashing). Numeral row: percentage *remaining* on the left
(`pct`, e.g. `"18%"`) and reset-in on the right (`reset`, reusing
`_format_countdown`, e.g. `"42m"`), sharing the row the way no other
overlay frame currently does (the running badge has only one numeral).

**Headroom theming** (background gradient, track-fill, title, and numeral
color all keyed off the same computed `remaining_pct`):

| Headroom | Remaining | `bg` gradient | `title` | `track_fill` | numerals |
|---|---|---|---|---|---|
| High | > 50% | `#031F17` → `#000A08` | `#6FFFCF` | `#33FFC1` | `#7CFFE0` |
| Medium | 20-50% (inclusive both ends) | `#231400` → `#0A0400` | `#FFCB6B` | `#FFB300` | `#FFD98C` |
| Low | < 20% | `#2E0509` → `#0A0101` | `#FF6B7A` | `#FF3B4E` | `#FF8A96` |

The 50% boundary belongs to "medium" and the 20% boundary also belongs to
"medium" -- i.e. "low" requires headroom to have genuinely dropped below
20%, not merely reached it. All three palettes follow the v1.4
near-black-gradient + bright-saturated-text contrast principle used
throughout this codebase.

**Round-robin and shape-change clears.** `OVERLAY_FRAME_SHAPE` maps
`ci_badge -> "badge"` and both quota frames `-> "quota"`: the badge's
element id set (`eta`) differs from the quota frames' (`pct`, `reset`),
so switching from the badge to either quota frame needs an explicit
`client.clear(APP)` first -- the same upsert-by-id firmware behavior that
required the v1.3.1 calendar transition-clear fix. `quota_gql` and
`quota_rest` share an identical id set, so switching between *those* two
needs no clear. `main.py` tracks the last-drawn shape and clears only on
an actual shape change, not on every dwell slot.

**On-device verification (real data, no fabrication for the quota
frames):** both quota frames built via the real `RestPoller.fetch_rate_limit`
-> `parse_rate_limit` -> `QuotaInfo` -> `build_overlay_payload` pipeline
(not hand-written payloads), drawn to `preview`, and passed the same
ink-overlap + buffer gate used for the running badge (title ⊆ rows 0-4,
`pct`+`reset` ⊆ rows 7-15, row 5 has no foreground ink, columns 0-1 have no
title ink). Real fetched data at capture time: REST bucket ~97-98%
remaining (high headroom, teal theme); GraphQL bucket varied across
captures from fully exhausted (0% remaining, low/red theme) to ~94%
remaining (high/teal theme) depending on real account activity between
runs -- both headroom tiers were exercised on live data, not synthesized.
Every element's `text` field was enumerated and confirmed to contain only
the bucket label, a percentage, or a countdown string -- no token,
username, or other account-identifying value ever appears in a quota
frame, structurally, since neither field is ever populated from anything
but the numeric `remaining`/`limit`/`reset` values. A separate on-device
run exercised the full 3-frame rotation end to end against the live
calendar agent: draw sequence `ci_badge -> quota_gql -> quota_rest ->
ci_badge`, landing at t=0s, 20.2s, 40.4s, 60.5s (each ~20s apart, matching
10s dwell + 10s gap per frame), all draws returning `200`; the calendar
reclaimed 3 of 4 sampled gap windows in that run, consistent with the
standalone ambient-tuning measurement above.

#### Config additions (final state)

`[ci_status]` gained, cumulative with the original round: `show_running =
true`, `running_poll_seconds = 20`, and now `show_quota = true`. `show_quota`
has no effect when `show_running` is false, since the quota frames only
ever appear as part of the running badge's own rotation. `calendar_countdown`'s
`poll_seconds` default changed from 60 to 10 (see the tuning table above);
existing configs that set `poll_seconds` explicitly are unaffected.

## 2026-08-03 — v1.5.1 account-wide repo watching

**Status:** Implemented, branch `dev/claude/account-wide-v1.5.1` off `main`
(which by this point already includes the full v1.5 revision above, merged
via PR #9). Not pushed.

New feature: `ci_status` can watch every repo the operator owns instead of
a fixed, manually-maintained `repos` list, with automatic pickup of newly
created (or newly re-activated) repos -- no config edit or restart needed.

### Config additions

`[ci_status]` gains: `watch_account_repos = false` (default off -- the
operator enables it locally, not shipped as a default, since it changes
what gets polled/displayed without an explicit repo list), `repos_exclude
= []`, `active_within_days = 30`, `repo_refresh_minutes = 60`.

### Discovery (`RestPoller.fetch_account_repos`, `ci_status/github.py`)

`GET /user/repos?affiliation=owner&sort=pushed&per_page=100`, paginated up
to a 1000-repo cap (10 pages). Only page 1 carries a conditional-request
(ETag) slot -- a deliberate choice: `sort=pushed` puts the most-recently-
active repos first, so the common case (a single-page account) gets a free
`304` whenever nothing relevant changed, while paying for pages 2+ on
every re-enumeration (itself only every `repo_refresh_minutes`, not every
poll) is an acceptable rare cost for >100-repo accounts. It also sidesteps
a correctness trap: reusing a cached page-2+ result on a page-1 304 could
miss a `pushed_at` update to a repo that moved within page 2 without
crossing into page 1 -- so pages 2+ are always fetched fresh when needed,
never cached across calls.

Returns `None` (not an empty list) on a 304 or any failure at any page --
an empty list is indistinguishable from "this account genuinely owns zero
repos," which would silently stop watching everything. The caller
(`main._refresh_account_repos`) treats `None` as "keep the previous
cached list," logging a warning only when there is no previous list to
fall back on yet.

### List resolution (`resolve_repo_list`, `ci_status/logic.py`)

Pure function: `repos` (explicit list, always included, never filtered by
recency) **union** auto-discovered account repos filtered to
non-archived + `pushed_at` within `active_within_days` **minus**
`repos_exclude` (applied unconditionally in both modes, always a no-op
when empty). Returns a sorted, deduplicated list -- the raw
`pushed`-sort order from discovery isn't meaningful to callers.

Caveat, documented in the README too: `pushed_at` is a repo-level field
with no notion of schedule-triggered workflow runs. A repo whose CI only
fires on a cron schedule (no pushes) ages out of `active_within_days` and
stops being watched even while its scheduled runs keep firing, since
nothing about a scheduled run touches `pushed_at`. Such a repo must be
named explicitly in `repos` to stay watched indefinitely.

### `run_once` integration (`ci_status/main.py`)

New optional `repo_cache` parameter (same caller-owned-mutable-dict
pattern as `running_cache`/`overlay_state`/`quota_cache`; omitting it
skips account-wide discovery entirely and preserves the exact pre-v1.5.1
behavior of polling `cfg["ci_status"]["repos"]` verbatim). Each poll
resolves the effective repo list fresh (cheap -- a set operation over
already-cached data, not a network call) via `resolve_repo_list`, calling
`_refresh_account_repos` (mirrors `_refresh_quota`'s freshness-cache
pattern) only when `watch_account_repos` is on and the cache is stale.

Any repo present in `state_cache`/`running_cache` from a previous poll but
absent from the freshly-resolved list -- excluded, aged out, or deleted
upstream -- has its cached state dropped, and the poller's own per-repo
ETag slots forgotten (`RestPoller.forget_repo`, pops both `_etags` and
`_running_etags`), so a stale failure/stuck alert or running badge can't
linger for a repo no longer being watched, and a repo that's later
re-added starts with a clean conditional-request slate.

Two related fixes needed for account-wide watching to actually work
end-to-end, not just compile: `main()`'s "no repos configured" validation
previously required a non-empty `repos` list unconditionally, which would
have rejected a valid `watch_account_repos = true` / `repos = []`
configuration outright -- now it only errors when *both* are empty/off.
`next_poll_seconds` previously iterated `cfg_ci["repos"]` to check for an
active run; since `running_cache`'s keys can now include auto-discovered
repos never present in `repos` at all, that would have silently failed to
shorten the poll interval for an active run on any auto-discovered repo,
defeating a chunk of the alternation feature for exactly the repos this
feature exists to add. Fixed to check `running_cache.values()` directly.

### Quota math and the private-repo-name caveat

See the README's "Account-wide watching" section for the full quota-cost
breakdown (N watched repos x `poll_seconds` cadence, all free in the
304 steady state; +1 request/`repo_refresh_minutes` for enumeration
itself, also ETag-cached on its first page) and the explicit note that
discovery includes private repos by design (no way to filter public vs.
private from the API call used, and it's fine for this integration's
local-only threat model) -- with the practical consequence that a private
repo's name can render on the physical display exactly like a public
one's.

### Verification

Tests: `resolve_repo_list` (union/exclude/active-window filtering,
explicit repos never filtered, empty/`None` account_repos, dedup +
deterministic sort), `fetch_account_repos` (single-page pass-through,
pagination across multiple pages, 304 handling, failure returns `None`
not `[]`, `forget_repo` clearing both ETag dicts), `_refresh_account_repos`
timing (stale cache re-fetches, fresh cache doesn't, failure keeps the
previous list), `state_cache`/`running_cache` pruning for repos dropped
from the effective list, `next_poll_seconds` checking auto-discovered
repos, config defaults. Full suite run verbatim in the implementation
report.

Live check: `--once --dry-run` against a **local throwaway config** (not
the operator's real `config.toml`) with `watch_account_repos` temporarily
`true`, confirming real discovery + resolution end-to-end against the
live GitHub API. Discovered-repo count and public-repo names are in the
implementation report; private repo names are redacted there (`<private-N>`)
since reports may be quoted in public PRs, even though the display itself
has no such redaction (see the caveat above).
