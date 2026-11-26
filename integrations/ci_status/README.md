# CI Status Integration

## What It Does

This integration monitors GitHub Actions workflows across your repositories and displays CI status on the busybar device. **Failure/stuck frames (v1.7):** when a workflow fails or a queued run goes stale (stuck due to offline runners or capacity), the device doesn't take over the panel — instead, a full-panel card for it joins the same calm **overlay-tier** rotation described below: a dark two-row card with a fixed `CI FAIL` heading (coral divider) for a failure, or `CI WAIT` (amber divider) for a stale-queued run, over a bold white line that scrolls `owner/repo #42 workflow` (the `#42` is the PR number, falling back to the branch name when a run has no PR, and dropped entirely when neither is available). One frame per failing/stuck run, alternating with the calendar, the running badge, and the quota frames — not a priority-60 takeover. A gentle red LED stays lit while a workflow is failing (stuck-only states don't light it), and turns off on the poll where the last failure clears.

**While a run is actively in progress** (and nothing is failing or stuck), the device shows a rotating set of **overlay-tier** frames instead: a cyan/blue "running" badge (repo, PR number or branch, and workflow name across the top; an ETA countdown below; a thin progress line tracking elapsed time against the workflow's typical duration), followed by two GitHub API quota frames (`show_quota`) if enabled. All of these frames share one dwell/gap rotation with the ambient-tier `calendar_countdown` integration — see "Display Priority Tiers" below for the shared framework this is built on, and "Overlay Rotation: Failure, Stuck, Running Badge, and Quota Frames" for content, config, and the measured alternation rhythm.

## Requirements

- **Python 3.12+**, `uv` package manager, and **GitHub CLI** installed and authenticated via `gh auth login` (platform-independent; runs on any OS; the integration reuses your existing auth token, stored securely by GitHub CLI)
- **Optional: macOS for autostart.** The LaunchAgent autostart packaging is macOS-specific; manual runs of the integration work on any OS with Python 3.12 + gh CLI
- **Device reachable** on your LAN (default `10.0.4.20` over USB-Ethernet; configurable for Wi-Fi)
- **GitHub repositories** with GitHub-hosted and/or self-hosted runners (both are fully supported)

## Design: REST-only, Quota-Efficient

The integration uses the **GitHub REST API only** (no GraphQL) to maintain strict quota isolation between CI status and other GitHub API consumers. This design choice ensures that CI monitoring never interferes with other API quota pools.

**Steady-state quota overhead is near zero.** The integration leverages **ETag/304 conditional requests**: an HTTP request is sent at each poll interval (e.g., every 120 seconds), but when no workflow state has changed, GitHub returns a cached `304 Not Modified` response, which does not consume REST API quota. Therefore, quota is spent only when CI state actually changes (workflows complete, fail, or queue transitions occur), not on every polling cycle.

Each poll sends up to three conditional requests per watched repo, each with its own ETag: the newest 10 runs across all branches, the newest 100 runs of the default branch (see "Which Runs Decide a Failure Card"), and, with `show_running`, the runs in progress. GitHub is asked only at `poll_seconds` / `running_poll_seconds`; the display rotates on its own faster tick without sending GitHub anything (see "Overlay Rotation").

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
watch_account_repos = false    # auto-discover and watch every repo you own (default: false) --
                                # see "Account-wide watching" below before enabling
```

At minimum, set `repos` to the repositories you want to monitor (e.g., `["owner/repo1", "owner/repo2"]`) -- unless you enable `watch_account_repos` instead (see below), in which case `repos` is optional and just adds always-included repos on top of whatever's auto-discovered.

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
| `poll_seconds` | integer | 120 | How often GitHub is asked, in seconds. Does not affect how often cards rotate. |
| `repos` | array of strings | — | GitHub repositories to monitor in `owner/repo` format. Required unless `watch_account_repos` is true, in which case these are always-included repos layered on top of auto-discovery (never filtered by `active_within_days`, since you named them explicitly). |
| `show_green` | boolean | false | Display successful/green workflow status (default: off to reduce noise) |
| `stale_queued_minutes` | integer | (disabled) | Alert if a workflow run has been queued for N minutes without starting (optional; useful to catch offline self-hosted runners) |
| `show_running` | boolean | true | Show the running-CI badge while a run is `in_progress` (across all configured repos; most-recently-started wins, `+N` if others are also running) |
| `running_poll_seconds` | integer | 20 | Poll interval while a run is active (shortened from `poll_seconds`) |
| `show_quota` | boolean | true | Join two GitHub API quota frames (GraphQL, REST) to the overlay rotation while a run is active. No effect if `show_running` is false — the quota frames only ever appear as part of that same rotation. |
| `running_spinner` | boolean | true | Show an animated 8×8 spinner in the lower-right corner of the running badge (x=64, y=8). The title keeps its full width; the ETA text is limited to 60 pixels to leave the spinner a clear lane. Set false for text-only running badge. No effect if `show_running` is false. |
| `bitmap_icons` | boolean | true | Add a 7×7 status icon beside the `CI FAIL` / `CI WAIT` / `CI OK` heading when the device's local firmware supports bitmap elements (1.2.3 and later). Purely cosmetic: without it, or on older firmware or the cloud route, the same card is drawn with text only. |
| `watch_account_repos` | boolean | false | Auto-discover and watch every repo you own, in addition to `repos`. See "Account-wide watching" below. |
| `repos_exclude` | array of strings | `[]` | Repos to never watch, regardless of mode — silences a specific repo without leaving account mode (or, less commonly, without editing `repos`). Applied last, unconditionally; a no-op when empty. |
| `active_within_days` | integer | 30 | In account mode, only auto-discovered repos pushed within this many days are watched (caps request volume on large accounts). Repos in `repos` are never subject to this filter. |
| `repo_refresh_minutes` | integer | 60 | How often the account's repo list is re-enumerated. A newly created (or newly pushed-to, if previously outside the active window) repo is picked up within this interval, not instantly. |

## Account-wide watching

By default this integration watches exactly the repos listed in `repos`. Setting `watch_account_repos = true` switches to a broader mode: the watch list becomes every repo you own (`GET /user/repos?affiliation=owner`, so this does **not** pick up repos you merely have collaborator/org-member access to, only ones under your own account) that's been pushed to within `active_within_days` days, **union** `repos` (always included, never filtered by recency), **minus** `repos_exclude`. New repos are picked up automatically — no config edit needed — within `repo_refresh_minutes` of their creation or of a first push that puts them back inside the active window.

**Private repos are included, and that's intentional.** Discovery has no way to filter private vs. public — it watches everything you own that's active. This is fine for this integration's threat model: both the resulting config state (the discovered list itself, cached in memory) and the physical display are local to your own device and your own account's token. But the practical consequence is real: **a private repo's name can render on the physical display** (in the running badge's title, or in a failure/stuck frame's `owner/repo #PR workflow` text) exactly like a public one would. If the device sits somewhere visible to people who shouldn't know a private repo exists, either keep `watch_account_repos` off and list repos explicitly, or add sensitive ones to `repos_exclude`.

**Quota math.** With N repos in the effective watch list, each poll cycle costs 2N REST requests to `.../actions/runs` — one for all branches and one for the default branch (steady-state, these return `304` and cost nothing against your quota — see "Design: REST-only, Quota-Efficient" above) — at `poll_seconds` cadence (default every 120s, so 2N requests every 2 minutes = up to `N * 60` requests/hour, all free in the steady state), plus N more to the running-runs endpoint whenever `show_running` is on; all three follow `running_poll_seconds` while any run is active. A default-branch body (up to 100 runs, roughly 1 MB) is downloaded only when a run on that branch changes. The default branch name comes from the account enumeration at no extra cost; a repo named in `repos` that your account does not own costs one more conditional request per `repo_refresh_minutes`. Account-wide discovery itself adds one more request per `repo_refresh_minutes` (default hourly = 1 request/hour, also ETag-cached on its first page — see `RestPoller.fetch_account_repos`'s docstring). None of this touches your real GitHub REST quota unless workflow state is actually changing, since 304s are free; the practical cap that matters is request *volume* (GitHub does rate-limit request rate, not just quota), which is why `active_within_days` exists — it keeps N bounded to your actually-active repos instead of every repo you've ever created.

**Caveat: `active_within_days` filters on `pushed_at`, a repo-level field — it has no idea about *schedule*-triggered workflow runs.** A repo whose CI only ever runs on a cron schedule (no pushes) will fall out of the active window and stop being watched even while its scheduled runs keep firing, because nothing about a scheduled run touches `pushed_at`. If you rely on schedule-triggered CI on a repo that doesn't otherwise see regular pushes, add it to `repos` explicitly (explicit repos are never subject to the active-window filter) rather than relying on account-wide discovery to keep watching it.

## Display Priority Tiers

This integration's failure/stuck/quiet-green frames and its running-badge
and quota frames all draw through the same shared overlay tier in
`src/busybar/display.py`, along with two firmware facts (measured, not
assumed — see the design spec's "Display tier framework" section for the
probe that found them):

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

**v1.7: no more alert-tier preemption.** A failure or stuck-queue run no longer
draws at a separate, higher `PRIORITY_ALERT` (60) tier and no longer
unconditionally wins the panel — that tier and its `build_ci_payload`
failure > stuck > overlay > quiet green > nothing precedence chain are
gone. Instead, one frame per failing run, then one per stuck run, is
prepended to the same ordered overlay sequence as the running badge and
quota frames (see `build_overlay_sequence` in `logic.py`), and the whole
sequence shares the overlay tier's usual dwell/silence rotation with
`calendar_countdown`. A CI failure therefore behaves like any other
overlay-tier frame: it takes its turn in the rotation rather than camping
the panel, and `calendar_countdown`'s escalation into
`PRIORITY_AMBIENT_RAISED` (25) or `PRIORITY_AMBIENT_URGENT` (65) already
sits strictly above it, so an approaching or imminent event naturally
outranks a CI failure with no special-case handling on this integration's
side. See `calendar_countdown`'s README for the full eviction interplay
and the priority table.

## Overlay Rotation: Failure, Stuck, Running Badge, and Quota Frames

The device rotates through the overlay-tier frames in play this cycle, one
per dwell slot (`OVERLAY_DWELL_SECONDS`, 10s), before repeating: one frame
per currently-failing run, then one per currently-stuck run, then the
running badge (if a run is `in_progress`), then each available quota
frame, then — only when nothing else is present and `show_green` is
on — a single quiet "CI ok" frame. See the design spec
(`docs/superpowers/specs/2026-08-06-animation-accents-design.md`) for the
running spinner implementation details.

**The rhythm does not depend on how often GitHub is polled.** The agent
wakes once a second and draws the next card as soon as it is due: one card
for 10s, then at least 10s in which it sends the device nothing so the
calendar can reclaim the panel, then the next card. That holds with nothing
running (`poll_seconds`, 120s by default) exactly as it does during a run
(`running_poll_seconds`); between polls the cards come from the last poll's
result, and the running badge's countdown is recomputed for each draw. A
wake-up with nothing due touches neither GitHub nor the device. A poll and a
draw do not overlap: a card that falls due while GitHub is being asked is
drawn as soon as the poll ends (usually well under a second; up to a few
seconds per repo while GitHub is unreachable), and its 10s on and 10s of
silence are counted from that moment. When there
is nothing to show, the device is cleared once per poll, not once per
wake-up. If the device refuses a card (the calendar is holding a higher
tier) the same card is offered again 20s later, and if the device is
unreachable the agent backs off from 5s up to 5 minutes between attempts.

### Which Runs Decide a Failure Card

Two views of each repo are kept apart:

- **The default branch.** For each workflow, the newest default-branch run
  that has a verdict decides. A `failure`, `timed_out` or `startup_failure`
  puts up a `CI FAIL` card; it stays until that workflow finishes with
  `success`, `neutral` or `skipped` on the default branch. Nothing else
  takes it down: not the same workflow passing on a pull request or another
  branch, not any number of newer runs, and not a re-run that is still
  queued or in progress, or one that was cancelled.
- **Pull requests and other branches.** The newest run of each workflow
  among the repo's 10 most recent runs decides, as before: a failing PR or
  branch run shows until a newer run of that workflow replaces it or it
  leaves those 10.

Stuck (`CI WAIT`) runs are found in the 10 most recent runs on any branch.

The verdicts are held in memory. After a restart the agent reads the newest
100 default-branch runs, so a failure older than that is not shown again
until the workflow next runs. A workflow that is deleted while failing on
the default branch keeps its card until the agent restarts, because it can
never produce a passing run. A pull request from a fork whose branch has the
same name as your default branch is treated as a pull request.

**When the default branch is not known.** A repo that the account
enumeration does not list is looked up once (`GET /repos/{owner}/{repo}`),
then re-checked every `repo_refresh_minutes` to notice a rename. If that
lookup fails, one warning is logged and it is retried every 5 minutes;
until it succeeds the repo is judged the old way — the newest run of each
workflow across all branches, from the 10 most recent — so its failures
still show, but a default-branch failure can be hidden by a newer run of
the same workflow elsewhere.

### Failure and Stuck Frames (v1.7)

Failure, waiting, and healthy frames share a dark two-row card. A fixed
`CI FAIL`, `CI WAIT`, or `CI OK` heading stays visible while the bold lower
row scrolls the complete `owner/repo #42 workflow` context. When several repos
are watched and they all share one owner, the owner is left out (`repo #42
workflow`, and likewise in the running badge's title) so the card opens on
the repository name instead of a prefix common to every card. Coral, amber, and
mint divider lines distinguish the states; firmware 1.2.3 adds a matching
7×7 status icon in the header. The healthy card reads `ALL CLEAR` without
scrolling. The PR number is replaced by the branch for push/fork runs, or
omitted if neither exists. Each failing or
stuck run gets its own frame in the rotation — with several failures or
stuck runs across repos, expect several `CI FAIL` / `CI WAIT` frames in a row before
the rotation reaches the running badge or quota frames.

A gentle red LED (`led_notification_color`) stays lit for the whole poll
cycle while any run is failing — it is **not** tied to whether a
failure/stuck frame happens to be the one currently on screen, and it is
**not** raised by a stuck-only state (no failures, only stale-queued
runs). The LED turns off explicitly on the exact poll where the last
failure clears, then stops being sent once already off.

### Running Badge and Quota Frames

While any configured repo has an `in_progress` run, the running badge and
(if enabled) two quota frames join the rotation after the failure/stuck
frames, if any:

1. **Running badge** (always first, always present when `show_running` is
   on): `REPO #PR WORKFLOW` (or `REPO branch-name WORKFLOW` for
   fork/push-triggered runs, which don't have a PR number) across the top,
   with `+N` appended if other runs are also active; an ETA below (`~4m`,
   `~1h05m` — reusing the calendar countdown's own formatter — or `soon`
   once the estimate is under a minute, or `3m in` when there's no
   successful-run history yet to estimate from); and a thin progress line
   tracking elapsed time against the workflow's typical duration (median
   of its last 5 successful runs, cached for the life of the process).
   When there's room, a small muted `remain` (or `left`, if `remain`
   doesn't fit) is appended right after the ETA — e.g. `~57m remain` or
   `~1h01m left` — never on `soon` (already imminent) or the no-history
   `3m in` form (that's elapsed time, not a remaining estimate, so a
   remaining-time label would be wrong, not just superfluous). Whether it
   fits at all, and which word if so, is a width-based decision (see
   `ci_status/logic.py`'s `_eta_label`); nothing to configure.
   
   **When `running_spinner` is true (default)**, the native 8×8 spinner
   occupies the lower-right corner. The title keeps its full 68-pixel width;
   the ETA and its label have a separate 60-pixel budget. A cyan-to-mint track
   and bright endpoint show elapsed progress. The device animates between
   ordinary polls; no host animation loop is added.
2. **GraphQL quota** (`show_quota`): title ribbon `GQL LEFT` with a separate `RESET` label, a track bar
   showing the fraction of the bucket used, and two numerals — percentage
   *remaining* on the left, reset-in on the right (e.g. `18%` / `42m`).
3. **REST quota** (`show_quota`): identical layout, title ribbon `REST LEFT` with the same `RESET` label.

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
color, and numeral color key off remaining-quota headroom, computed from
the same fetch. The title and `RESET` label are white at every level:

| Headroom | Remaining | Background gradient | Title / numeral / track-fill |
|---|---|---|---|
| High | > 50% | `#081B23` → `#020611` (teal-navy) | `#FFFFFF` / `#7CFFE0` / `#33FFC1` |
| Medium | 20–50% | `#211707` → `#020611` (amber-navy) | `#FFFFFF` / `#FFD98C` / `#FFB300` |
| Low | < 20% | `#240A10` → `#020611` (red-navy) | `#FFFFFF` / `#FF8A96` / `#FF3B4E` |

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

If `stale_queued_minutes` is set (e.g., `15`), the integration monitors how long runs sit in the queued state. When a run exceeds this threshold without starting, it indicates a capacity problem — often an **offline or unavailable self-hosted runner**. The device adds a `CI WAIT` card (amber divider) for that run to the overlay rotation so you can investigate the runner. The age is re-checked on every poll, so a run that sits untouched in the queue still crosses the threshold on time.

For example:
- Set `stale_queued_minutes = 15` to alert if any run has been queued for more than 15 minutes.
- Self-hosted runners that go offline will trigger this alert, helping you catch infrastructure issues before they block development.

## Autostart

### Install LaunchAgent

From the repository root, run these commands to install the CI integration as a background service that starts at login:

```bash
cd integrations/ci_status
mkdir -p ~/Library/Logs/busybar
sed -e "s|__REPO__|$(git rev-parse --show-toplevel)|" -e "s|__HOME__|$HOME|" \
  com.busybar.ci-status.plist > ~/Library/LaunchAgents/com.busybar.ci-status.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.busybar.ci-status.plist
```

The agent will start automatically at your next login and run continuously, polling your workflows at the interval specified in `config.toml`. The agent sets PYTHONPATH to the repo's src/ directory so the busybar package resolves even without a healthy editable install.

The agent runs the project's own virtual-environment interpreter (`.venv/bin/python`), so run `uv sync` from the repository root before installing and again whenever dependencies change. It deliberately does not launch through `uv run`: macOS ties a background agent's Local Network, Files and Calendar permissions, and launchd's own launch record, to the identity of the program it starts, and a package-manager upgrade of `uv` replaces that program. A running agent then loses access to the bar's USB address and to the repository folder, and cannot be restarted until it is re-registered. The interpreter under `.venv` only changes when Python itself is upgraded; after that, or after recreating `.venv`, re-register the agent with the uninstall and install commands below and approve the permission prompts again.

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

Each distinct summary line (a card that was drawn, the silent gap, nothing to show) is logged at `INFO` once per 10 minutes, so cards alternating every 20 seconds do not fill the log; repeats are logged at `DEBUG`. A repeating GitHub problem (an error status, GitHub unreachable, an unreadable response) is logged as a warning when it appears or changes, then as a reminder every 10 minutes — not once per repo per poll.

### When GitHub Does Not Answer

A `304 Not Modified` means the cached state is still current and is kept indefinitely. The same goes for any failure that only means the question could not be asked — GitHub unreachable (including a laptop that is offline or asleep), a `5xx` server error, a rate limit, or a response this integration cannot read: the failure/stuck frames, the running badge and the LED stay as they were for as long as it lasts, because nothing has been learned about the repo.

Only a refusal expires cached state: `401`, `404`, `410`, or a `403` that is not a rate limit. When GitHub has kept refusing a repo for 15 minutes, its failure/stuck frames and running badge are dropped (and the LED turned off if nothing else is failing), so a deleted or renamed repo, or a revoked token, cannot leave `CI FAIL` on the display. One warning lists every repo dropped in that poll. State returns on the next good answer. A revoked token needs `gh auth login` and an agent restart, since the token is read once at startup.

A rate-limit answer (`403`/`429` carrying `Retry-After`, or with the quota exhausted) pauses every GitHub request for the time GitHub asks, bounded to between 1 and 15 minutes; after the cap one probe request is sent, and the pause repeats if GitHub is still limiting. GitHub's primary limit can take up to an hour to reset. The display keeps rotating cached frames meanwhile.

The default-branch request follows the same rules. When it is answered `304`, cannot be asked, or is refused, the default-branch verdicts already held stay as they are; a new default-branch failure that appears in the all-branches request in the meantime is still shown. The verdicts are dropped only together with the rest of the repo's state, after 15 minutes of refusals on the all-branches request, or when the repo leaves the watch list. A poll in which the default branch name is unknown keeps them too.

With account-wide watching, the repo list is replaced only by a complete enumeration: if any page fails, the previous list is kept and enumeration is retried on the next poll.

With `show_green` on, the `CI OK` card is withheld until every watched repo has been read successfully, so it never claims "all clear" for a repo GitHub has not answered for.

## GitHub-Hosted and Self-Hosted Runners

Both runner types are fully supported and covered identically:

- **GitHub-hosted runners** (e.g., `ubuntu-latest`, `macos-latest`) are monitored like any other runner.
- **Self-hosted runners** (your own machines) are monitored identically. If a self-hosted runner goes offline, the `stale_queued_minutes` detection will alert you when runs start piling up in the queued state.
