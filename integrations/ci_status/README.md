# CI Status Integration

## What It Does

This integration monitors GitHub Actions workflows across your repositories and displays CI status on the busybar device. When workflows fail, the device shows a full-panel red badge (rounded background + bold white text) listing the affected `repo:workflow` pairs. When queued runs become stale (stuck due to offline runners or capacity), the device shows a full-panel amber badge with black text instead. Long lists scroll. The integration displays at priority 60, but an active BUSY session (priority 90) will override the display to show a blinking red status LED instead.

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

Stdout and stderr are redirected to `/tmp/busybar-ci.log`. View recent activity with:

```bash
tail -f /tmp/busybar-ci.log
```

## GitHub-Hosted and Self-Hosted Runners

Both runner types are fully supported and covered identically:

- **GitHub-hosted runners** (e.g., `ubuntu-latest`, `macos-latest`) are monitored like any other runner.
- **Self-hosted runners** (your own machines) are monitored identically. If a self-hosted runner goes offline, the `stale_queued_minutes` detection will alert you when runs start piling up in the queued state.
