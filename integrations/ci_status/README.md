# CI Status Integration

## What It Does

This integration monitors GitHub Actions workflows across your repositories and displays CI status on the busybar device. When workflows fail, the device shows a full-panel red badge (rounded background + bold white text) listing the affected `repo:workflow` pairs. When queued runs become stale (stuck due to offline runners or capacity), the device shows a full-panel amber badge with black text instead. Long lists scroll. The integration displays at the **alert** tier (`busybar.display.PRIORITY_ALERT`, priority 60), but an active BUSY session (priority 90) will override the display to show a blinking red status LED instead.

**While a run is actively in progress** (and nothing is failing or stuck), the device shows a rotating set of **overlay-tier** frames instead: a cyan/blue "running" badge (repo, PR number or branch, and workflow name across the top; an ETA countdown below; a thin progress line tracking elapsed time against the workflow's typical duration), followed by two GitHub API quota frames (`show_quota`) if enabled. These three frames share one dwell/gap rotation with the ambient-tier `calendar_countdown` integration — see "Display Priority Tiers" below for the shared framework this is built on, and "Overlay Rotation: Running Badge + Quota Frames" for content, config, and the measured alternation rhythm.

## Requirements

- **Python 3.12+**, `uv` package manager, and **GitHub CLI** installed and authenticated via `gh auth login` (platform-independent; runs on any OS; the integration reuses your existing auth token, stored securely by GitHub CLI)
- **Optional: macOS for autostart.** The LaunchAgent autostart packaging is macOS-specific; manual runs of the integration work on any OS with Python 3.12 + gh CLI
- **Device reachable** on your LAN (default `10.0.4.20` over USB-Ethernet; configurable for Wi-Fi)
- **GitHub repositories** with GitHub-hosted and/or self-hosted runners (both are fully supported)

## Design: REST-only, Quota-Efficient

The integration uses the **GitHub REST API only** (no GraphQL) to maintain strict quota isolation between CI status and other GitHub API consumers. This design choice ensures that CI monitoring never interferes with other API quota pools.

**Steady-state quota overhead is near zero.** The integration leverages **ETag/304 conditional requests**: an HTTP request is sent at each poll interval (e.g., every 120 seconds), but when no workflow state has changed, GitHub returns a cached `304 Not Modified` response, which does not consume REST API quota. Therefore, quota is spent only when CI state actually changes (workflows complete, fail, or queue transitions occur), not on every polling cycle.

## Setup

### 1. Authenticate GitHub CLI

If you haven't already, authenticate with GitHub:

```bash
gh auth login
```

Follow the interactive prompts and complete the authentication. The integration will automatically use your stored credentials.

### 2. Configure

Copy the example config to your repository root:

```bash
cp config.example.toml config.toml
```

Edit `config.toml` and configure the `[ci_status]` section:

```toml
[ci_status]
poll_seconds = 120             # how often to check workflows (default: 120)
repos = ["your-user/your-repo"]  # list of repos to monitor
show_green = false             # display green builds (default: false)
# stale_queued_minutes = 15    # optional: alert if runs stuck queued for N minutes
show_running = true            # show a badge while a run is in progress (default: true)
running_poll_seconds = 20      # poll interval while a run is active (default: 20)
show_quota = true              # GraphQL/REST quota frames join the overlay rotation while a
                                # run is active (default: true; no effect if show_running is false)
```

At minimum, set `repos` to the repositories you want to monitor (e.g., `["owner/repo1", "owner/repo2"]`).

### 3. Test in Foreground

From the repository root, run the integration once:

```bash
cd integrations
uv run python -m ci_status.main --once --dry-run
```

Verify that the output shows workflow status for your repositories. The `--dry-run` flag prints the status payload without sending it to the device. **This test run confirms your GitHub auth is working before automating.**

### 4. Verify Config

Once the foreground test completes, your `config.toml` is in place and GitHub auth is confirmed. The LaunchAgent installation step below will automate polling.

## Config Reference

| Key | Type | Default | Purpose |
|---|---|---|---|
| `poll_seconds` | integer | 120 | Polling interval in seconds |
| `repos` | array of strings | — | GitHub repositories to monitor in `owner/repo` format (required) |
| `show_green` | boolean | false | Display successful/green workflow status (default: off to reduce noise) |
| `stale_queued_minutes` | integer | (disabled) | Alert if a workflow run has been queued for N minutes without starting (optional; useful to catch offline self-hosted runners) |
| `show_running` | boolean | true | Show the running-CI badge while a run is `in_progress` (across all configured repos; most-recently-started wins, `+N` if others are also running) |
| `running_poll_seconds` | integer | 20 | Poll interval while a run is active (shortened from `poll_seconds`) |
| `show_quota` | boolean | true | Join two GitHub API quota frames (GraphQL, REST) to the overlay rotation while a run is active. No effect if `show_running` is false — the quota frames only ever appear as part of that same rotation. |

## Display Priority Tiers

This integration's alert badges (failure/stuck) and its overlay-tier
frames (running badge, quota frames) both draw through the shared
priority ladder in `src/busybar/display.py`, along with two firmware
facts (measured, not assumed — see the design spec's "Display tier
framework" section for the probe that found them):

- **Equal priority from a different `application_name` is rejected
  outright**, not treated as a hand-off, contrary to what the device's own
  API documentation claims. This is why the overlay tier lives at its own
  priority (`PRIORITY_OVERLAY`, 21) strictly above the calendar's ambient
  tier (`PRIORITY_AMBIENT`, 20) rather than reusing it.
- **A preempted app's elements are evicted, not restored.** Once an
  overlay-tier draw's own timeout expires, the panel goes dark; the
  calendar's last draw does not silently reappear underneath. The calendar
  only gets the screen back via its own next scheduled redraw landing in
  that dark gap — see `calendar_countdown`'s README ("Display Priority
  Tiers") for the tuning history and measured recovery rates.

The alert tier (`PRIORITY_ALERT`, 60) sits above the overlay tier and
preempts it unconditionally — a failure or stuck-queue badge always wins
over the running badge or a quota frame, per the precedence in
`build_ci_payload` (failure > stuck > overlay > quiet green > nothing).

## Overlay Rotation: Running Badge + Quota Frames

While any configured repo has an `in_progress` run (and nothing is failing
or stuck), the device rotates through up to three overlay-tier frames, one
per dwell slot (`OVERLAY_DWELL_SECONDS`, 10s), before repeating:

1. **Running badge** (always first, always present when `show_running` is
   on): `REPO #PR WORKFLOW` (or `REPO branch-name WORKFLOW` for
   fork/push-triggered runs, which don't have a PR number) across the top,
   with `+N` appended if other runs are also active; an ETA below (`~4m`,
   `~1h05m` — reusing the calendar countdown's own formatter — or `soon`
   once the estimate is under a minute, or `3m in` when there's no
   successful-run history yet to estimate from); and a thin progress line
   tracking elapsed time against the workflow's typical duration (median
   of its last 5 successful runs, cached for the life of the process).
2. **GraphQL quota** (`show_quota`): title ribbon `GITHUB GRAPHQL`, a track bar
   showing the fraction of the bucket used, and two numerals — percentage
   *remaining* on the left, reset-in on the right (e.g. `18%` / `42m`).
3. **REST quota** (`show_quota`): identical layout, title ribbon `GITHUB REST`.

Each quota frame is built from a single `GET /rate_limit` call, fetched
fresh once per `running_poll_seconds` cycle while a run is active — this
endpoint is explicitly **exempt from GitHub's own rate limiting**, so
polling it does not consume any other quota pool. If that fetch fails, or
the last successful fetch is more than 5 minutes stale, the quota frames
are silently dropped from that cycle's rotation (never a crash, never
stale numbers on screen) — the running badge keeps rotating on its own.
Percentages and reset countdowns are the only numbers shown; no token,
username, or other account-identifying text ever appears in a quota
frame (both fields are computed purely from the numeric `remaining` /
`limit` / `reset` values in the API response).

**Headroom theming.** Each quota frame's background gradient, track-fill
color, title color, and numeral color all key off remaining-quota
headroom, computed from the same fetch:

| Headroom | Remaining | Background gradient | Title / numeral / track-fill |
|---|---|---|---|
| High | > 50% | `#031F17` → `#000A08` (teal-black) | `#6FFFCF` / `#7CFFE0` / `#33FFC1` |
| Medium | 20–50% | `#231400` → `#0A0400` (amber-black) | `#FFCB6B` / `#FFD98C` / `#FFB300` |
| Low | < 20% | `#2E0509` → `#0A0101` (red-black) | `#FF6B7A` / `#FF8A96` / `#FF3B4E` |

**Important: none of this alternates cleanly with the calendar**, for the
same firmware reasons as the running badge alone did before quota frames
existed — see "Display Priority Tiers" above. In practice, while CI is
running, expect the panel to spend roughly half its time showing an
overlay-tier frame (running badge or a quota frame) and the rest either
dark or reclaimed by the calendar, not a clean three-way handoff. On-device
re-measurement after tuning the calendar's own poll interval to 10s (see
`calendar_countdown`'s README for the full three-round table) found the
calendar recovering 4 of 6 sampled dwell gaps in a standalone measurement,
and 3 of 4 gap windows in a separate run that exercised the full 3-frame
rotation end to end — draw sequence `ci_badge → quota_gql → quota_rest →
ci_badge`, each landing ~20s apart (10s dwell + 10s gap), confirmed
against the live device. This is a known limitation of the current
zero-cross-process-coordination design, not a bug; the fixed 10s dwell
(`OVERLAY_DWELL_SECONDS` in `src/busybar/display.py`) and the calendar's
own `poll_seconds` are the two knobs that shape the ratio.

### Stale Queued Detection

If `stale_queued_minutes` is set (e.g., `15`), the integration monitors how long runs sit in the queued state. When a run exceeds this threshold without starting, it indicates a capacity problem — often an **offline or unavailable self-hosted runner**. The device displays a yellow "CI stuck" alert to notify you to investigate the runner.

For example:
- Set `stale_queued_minutes = 15` to alert if any run has been queued for more than 15 minutes.
- Self-hosted runners that go offline will trigger this alert, helping you catch infrastructure issues before they block development.

## Autostart

### Install LaunchAgent

From the repository root, run these commands to install the CI integration as a background service that starts at login:

```bash
cd integrations/ci_status
mkdir -p ~/Library/Logs/busybar
sed -e "s|__REPO__|$(git rev-parse --show-toplevel)|" -e "s|__UV__|$(command -v uv)|" -e "s|__HOME__|$HOME|" \
  com.busybar.ci-status.plist > ~/Library/LaunchAgents/com.busybar.ci-status.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.busybar.ci-status.plist
```

The agent will start automatically at your next login and run continuously, polling your workflows at the interval specified in `config.toml`. The agent sets PYTHONPATH to the repo's src/ directory so the busybar package resolves even without a healthy editable install.

### Uninstall LaunchAgent

To stop the service and remove it from autostart:

```bash
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.busybar.ci-status.plist
rm ~/Library/LaunchAgents/com.busybar.ci-status.plist
```

## Logs

Stdout and stderr are redirected to `~/Library/Logs/busybar/ci.log`. View recent activity with:

```bash
tail -f ~/Library/Logs/busybar/ci.log
```

## GitHub-Hosted and Self-Hosted Runners

Both runner types are fully supported and covered identically:

- **GitHub-hosted runners** (e.g., `ubuntu-latest`, `macos-latest`) are monitored like any other runner.
- **Self-hosted runners** (your own machines) are monitored identically. If a self-hosted runner goes offline, the `stale_queued_minutes` detection will alert you when runs start piling up in the queued state.
