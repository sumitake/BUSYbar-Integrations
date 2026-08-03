# Calendar Countdown Integration

## What It Does

This integration polls your macOS calendar for upcoming events and displays a live countdown on the busybar device. As each event approaches, the display updates in real time, showing the event time and minutes remaining. For example: `14:00 Standup in 23m`.

The integration looks ahead 12 hours by default, surfaces events within a configurable warning window (5 minutes by default), and draws them at priority 20 on the display. If an active BUSY session exists on the device, the calendar event display is suppressed in favor of the busy state (priority 90).

## Requirements

- **macOS 10.14+** with Calendar app enabled
- **Calendar permission granted** to Claude Code (must be answered once in a foreground terminal run before automating)
- **Device reachable** on your LAN (default `10.0.4.20` over USB-Ethernet; configurable for Wi-Fi)
- **Python 3.9+** and `uv` package manager

## Setup

### 1. Configure

Copy the example config to your repository root:

```bash
cp config.example.toml config.toml
```

Edit `config.toml` and configure the `[calendar_countdown]` section:

```toml
[calendar_countdown]
poll_seconds = 60              # how often to check the calendar (default: 60)
lookahead_hours = 12           # how far ahead to scan (default: 12)
warn_minutes = 5               # show countdown when within N minutes (default: 5)
include_all_day = false        # include all-day events (default: false)
auto_busy = false              # auto-mark as BUSY during events (default: false)
# calendars = ["Work"]         # optional: limit to specific calendars
```

### 2. Test in Foreground

From the repository root, run the integration once to answer the calendar-permission prompt:

```bash
cd integrations
uv run python -m calendar_countdown.main --once --dry-run
```

Verify that the output shows upcoming events. **This foreground run is required** before installing the LaunchAgent — the permission prompt appears in the terminal and cannot be answered by a background agent.

### 3. Verify Config

Once the foreground test completes, your `config.toml` is in place and the permission is granted. The LaunchAgent installation step below will automate polling.

## Config Reference

| Key | Type | Default | Purpose |
|---|---|---|---|
| `poll_seconds` | integer | 60 | Polling interval in seconds |
| `lookahead_hours` | integer | 12 | Hours into the future to scan for events |
| `warn_minutes` | integer | 5 | Show countdown when within N minutes of event start |
| `include_all_day` | boolean | false | Include all-day events in the display |
| `auto_busy` | boolean | false | Automatically mark device BUSY during event time |
| `calendars` | array of strings | (all) | Comma-separated list of calendar names to monitor (omit to read all) |

## Autostart

### Install LaunchAgent

Run this command to install the calendar integration as a background service that starts at login:

```bash
sed -e "s|__REPO__|$(git rev-parse --show-toplevel)|" -e "s|__UV__|$(command -v uv)|" \
  com.busybar.calendar-countdown.plist > ~/Library/LaunchAgents/com.busybar.calendar-countdown.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.busybar.calendar-countdown.plist
```

The agent will start automatically at your next login and run continuously, polling your calendar at the interval specified in `config.toml`.

### Uninstall LaunchAgent

To stop the service and remove it from autostart:

```bash
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.busybar.calendar-countdown.plist
rm ~/Library/LaunchAgents/com.busybar.calendar-countdown.plist
```

## Logs

Stdout and stderr are redirected to `/tmp/busybar-calendar.log`. View recent activity with:

```bash
tail -f /tmp/busybar-calendar.log
```
