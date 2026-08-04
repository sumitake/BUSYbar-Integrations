# Calendar Countdown Integration

## What It Does

This integration polls your macOS calendar for upcoming events and displays a live countdown card on the busybar device: a full-panel gradient background that itself signals urgency, a title row, a horizontal drain track, a large start-time (or "ENDS") card, and a native on-device countdown timer that ticks every second between polls.

- **Color Horizon background** — the whole panel is a top-to-bottom gradient whose colors shift with urgency: deep violet when the event is far off, warm orange approaching `notice_minutes`, red approaching `warn_minutes`, and teal while the event is in progress. Urgency reads at a glance before you even look at the countdown.
- **Title row** — the event title in a small font across the top. Long titles scroll; short ones sit static.
- **Drain track** — a thin horizontal bar under the title fills proportionally to time remaining within `progress_window_minutes` (default 60) and shrinks toward empty as the start time arrives. An in-progress event shows it full-width and steady instead (no drain — the relevant countdown is now "time until it ends").
- **Time / Ends card** — a large `HH:MM` local start time on a rounded card for an upcoming event, or a bold "ENDS" label once the event has begun.
- **Countdown card** — a native device countdown, right-anchored in its own rounded card: to the event start while upcoming, or to its end once in progress, so the display keeps ticking without waiting on the next poll.
- **Four states** — `normal`, `notice` (within `notice_minutes` of start), `warning` (within `warn_minutes` of start), and `in_progress`, each with its own background gradient, title color, drain-track gradient, divider color, countdown-card color, and digit color. See the design spec (`docs/superpowers/specs/2026-08-03-calendar-ci-integrations-design.md`) for the full palette table.

The integration looks ahead 12 hours by default and draws at priority 20 on the display. If an active BUSY session exists on the device, the calendar event display is suppressed in favor of the busy state (priority 90).

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
poll_seconds = 60              # how often to check the calendar (default: 60)
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
| `poll_seconds` | integer | 60 | Polling interval in seconds |
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
