# Calendar Countdown Integration

## What It Does

This integration polls your macOS calendar for upcoming events and displays a live countdown on the busybar device: a full-panel gradient background that itself signals urgency, an uppercase title row, a horizontal drain track, and large numerals (start time, or countdown) floating directly on the background — no card surfaces, for an airy, high-contrast look.

- **Color Horizon background** — the whole panel is a top-to-bottom gradient whose colors shift with urgency: deep violet when the event is far off, warm orange approaching `notice_minutes`, red approaching `warn_minutes`, and teal while the event is in progress. Urgency reads at a glance before you even look at the countdown.
- **Title row** — the event title, uppercased, in a small font flush across the top. Long titles scroll; short ones sit static.
- **Drain track** — a thin horizontal line spanning the full width, edge to edge, below the title. It fills proportionally to time remaining within `progress_window_minutes` (default 60) and shrinks toward empty as the start time arrives. An in-progress event shows it full-width and steady instead (no drain — the relevant countdown is now "time until it ends").
- **Time / Ends** — a large local `HH:MM` start time for an upcoming event, or a bold "ENDS" label once the event has begun. No card behind either — both float directly on the gradient background.
- **Countdown** — a large minutes-granular countdown, same size as the time/ends numeral (they always change together): `"54m"` under an hour, `"1h05m"` at/above an hour, falling back to hour-only (`"9h"`) whenever the combined form would run too wide for the space available — which font-width measurement shows can happen even for some single-digit-hour values, not just at 10+ hours. Counts to the event start while upcoming, or to its end once in progress; re-rendered each poll rather than ticking natively on-device, so it updates on the same cadence as the rest of the display (`poll_seconds`).
- **Four states** — `normal`, `notice` (within `notice_minutes` of start), `warning` (within `warn_minutes` of start), and `in_progress`, each with its own background gradient, title color, drain-track gradient, divider color, and digit color. See the design spec (`docs/superpowers/specs/2026-08-03-calendar-ci-integrations-design.md`) for the full palette table and row-budget diagram.

The integration looks ahead 12 hours by default and draws at the ambient tier (`busybar.display.PRIORITY_AMBIENT`, priority 20) on the display. If an active BUSY session exists on the device, the calendar event display is suppressed in favor of the busy state (priority 90). If the `ci_status` integration is also running with `show_running` and/or `show_quota` enabled, the calendar and the overlay rotation (running badge, GraphQL/REST quota frames) trade the screen back and forth for as long as a CI run is active — see `ci_status`'s README ("Display Priority Tiers" / alternation rhythm) for the measured numbers; the panel still goes fully dark for a few seconds in most cycles because the firmware never restores an occluded element on its own (see "Display Priority Tiers" below), but at the tuned 10s ambient poll the calendar now recovers the screen in roughly 3 of every 4 overlay dwell gaps rather than effectively never.

## Requirements

- **macOS 14+ (Sonoma)** with Calendar app enabled
- **Calendar permission granted** in System Settings > Privacy & Security > Calendars (must be answered once in a foreground terminal run before automating)
- **Device reachable** on your LAN (default `10.0.4.20` over USB-Ethernet; configurable for Wi-Fi)
- **Python 3.12+** and `uv` package manager

## Setup

### 1. Grant Calendar Permission

From the repository root, run the integration once to answer the calendar-permission prompt:

```bash
cd integrations
uv run python -m calendar_countdown.main --once --dry-run
```

This foreground run is required before automating — the permission prompt appears in the terminal and cannot be answered by a background agent. After the permission dialog, press ^C to exit.

### 2. Discover Calendar Names

To limit polling to specific calendars, first discover the exact names available on your system:

```bash
uv run python -m calendar_countdown.main --list-calendars
```

This prints the calendar names organized by account. Use these exact names in the config.

### 3. Configure

Copy the example config to your repository root:

```bash
cp config.example.toml config.toml
```

Edit `config.toml` and configure the `[calendar_countdown]` section:

```toml
[calendar_countdown]
poll_seconds = 10              # how often to check the calendar (default: 10 -- ambient-tier
                                # redraw cadence; see "Display Priority Tiers" below)
lookahead_hours = 12           # how far ahead to scan (default: 12)
warn_minutes = 5               # bar/countdown turn red within N minutes of start (default: 5)
notice_minutes = 15            # bar/countdown turn amber within N minutes of start (default: 15)
progress_window_minutes = 60   # drain track empties from full over this many minutes (default: 60)
include_all_day = false        # include all-day events (default: false)
auto_busy = false              # auto-mark as BUSY during events (default: false)
# calendars = ["Work"]         # optional: list calendar names to limit scope (discover with --list-calendars)
```

### 4. Test Live

Once config is in place, test the integration with a dry-run:

```bash
uv run python -m calendar_countdown.main --once --dry-run
```

Verify that the output shows your next upcoming event with the correct countdown.

## Config Reference

| Key | Type | Default | Purpose |
|---|---|---|---|
| `poll_seconds` | integer | 10 | Polling interval in seconds (ambient-tier redraw cadence; raise it if 10s polling is more than your calendar setup needs, but see "Display Priority Tiers" below for the alternation tradeoff) |
| `lookahead_hours` | integer | 12 | Hours into the future to scan for events |
| `warn_minutes` | integer | 5 | Bar/countdown turn red when within N minutes of event start |
| `notice_minutes` | integer | 15 | Bar/countdown turn amber when within N minutes of event start |
| `progress_window_minutes` | integer | 60 | Drain track empties from full width over this many minutes before the event start |
| `include_all_day` | boolean | false | Include all-day events in the display |
| `auto_busy` | boolean | false | Automatically mark device BUSY during event time |
| `calendars` | array of strings | (all) | List of calendar names to monitor. Discover available names with `uv run python -m calendar_countdown.main --list-calendars` |

## Autostart

### Install LaunchAgent

From the repository root, run these commands to install the calendar integration as a background service that starts at login:

```bash
cd integrations/calendar_countdown
mkdir -p ~/Library/Logs/busybar
sed -e "s|__REPO__|$(git rev-parse --show-toplevel)|" -e "s|__UV__|$(command -v uv)|" -e "s|__HOME__|$HOME|" \
  com.busybar.calendar-countdown.plist > ~/Library/LaunchAgents/com.busybar.calendar-countdown.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.busybar.calendar-countdown.plist
```

The agent will start automatically at your next login and run continuously, polling your calendar at the interval specified in `config.toml`. The agent sets PYTHONPATH to the repo's src/ directory so the busybar package resolves even without a healthy editable install.

### Uninstall LaunchAgent

To stop the service and remove it from autostart:

```bash
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.busybar.calendar-countdown.plist
rm ~/Library/LaunchAgents/com.busybar.calendar-countdown.plist
```

## Logs

Stdout and stderr are redirected to `~/Library/Logs/busybar/calendar.log`. View recent activity with:

```bash
tail -f ~/Library/Logs/busybar/calendar.log
```

At the default 10s poll cadence, most polls redraw the same event unchanged. To avoid multiplying the log's line rate versus the old 60s cadence sixfold, draw summaries are logged at `INFO` only when the summary actually changes or every 10 minutes (a heartbeat line), and at `DEBUG` otherwise -- `DEBUG` isn't emitted by the default log level, so routine unchanged polls don't appear in `calendar.log` at all. Run with `python -m logging` verbosity raised, or check the process's own stdout in the foreground, if you need to see every single poll.

## Display Priority Tiers

This integration draws at the **ambient** tier (`busybar.display.PRIORITY_AMBIENT`, priority 20) -- see `src/busybar/display.py` for the full shared priority ladder used across every busybar integration, and the design spec's "Display tier framework" section for the two firmware facts (measured, not assumed) that shape it: a different app can only preempt this one with a strictly higher priority (equal priority is rejected outright), and once preempted, this app's elements are evicted rather than restored -- the calendar only gets the screen back via its own next scheduled redraw, never automatically. The 10s default poll interval exists specifically so those redraws happen often enough to reliably interleave with the `ci_status` overlay tier (priority 21, ~10s dwell gaps, now rotating through the running badge plus two quota frames -- see `ci_status`'s README) during an active CI run. On-device re-measurement across three poll settings, sampled every 2s for ~130s against the live agent:

| Calendar `poll_seconds` | Dwell cycles recovered | Sample split (BADGE / BLANK / OTHER=calendar) |
|---|---|---|
| 60 (pre-v1.5 baseline) | 0 of 6 | 33 / 31 / 0 |
| 15 | 2 of 6 | 34 / 26 / 5 |
| 10 (current default) | 4 of 6 | 35 / 20 / 10 |

Matching the poll to the dwell gap exactly (10s) did not eliminate the dark gaps entirely -- the two timers still run independently with no cross-process coordination, so recovery timing within a gap varies (observed roughly 2-8s into a given 10s gap) and 2 of the 6 sampled cycles still showed no recovery at all -- but it took the calendar from "never recovers" to "recovers in most cycles." A separate on-device run exercising the full 3-frame overlay rotation (running badge -> GraphQL quota -> REST quota -> repeat) at the same 10s dwell showed the same pattern: the calendar reclaimed 3 of the 4 gap windows sampled. If your setup still shows the panel dark for more than a few seconds at a stretch, that is consistent with this measurement, not a bug; lowering `poll_seconds` further has diminishing returns since the elements' own render/transmit latency puts a floor on how tightly the two timers can align.
