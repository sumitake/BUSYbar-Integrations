# BUSY Bar Integrations

[![ci](https://github.com/sumitake/busybar-integrations/actions/workflows/ci.yml/badge.svg)](https://github.com/sumitake/busybar-integrations/actions/workflows/ci.yml)
[![CodeQL](https://github.com/sumitake/busybar-integrations/actions/workflows/codeql.yml/badge.svg)](https://github.com/sumitake/busybar-integrations/actions/workflows/codeql.yml)
[![secret-scan](https://github.com/sumitake/busybar-integrations/actions/workflows/secret-scan.yml/badge.svg)](https://github.com/sumitake/busybar-integrations/actions/workflows/secret-scan.yml)
[![License: MPL-2.0](https://img.shields.io/badge/License-MPL--2.0-blue.svg)](LICENSE)

Local-API integrations for the BUSY Bar — a 72×16 LED status display on USB or LAN.

## Requirements

- **BUSY Bar** on USB (default address `10.0.4.20`) or LAN
- **Python 3.12+**
- **[uv](https://docs.astral.sh/uv/)** — fast Python package installer and resolver

Per-integration extras (e.g., macOS Calendar access for `calendar_countdown`) are noted in each integration's README.

## Quick start

1. Clone the repo:
   ```bash
   git clone https://github.com/your-org/busybar-integrations.git
   cd busybar-integrations
   ```

2. Sync dependencies:
   ```bash
   uv sync
   ```

3. Copy and edit the configuration:
   ```bash
   cp config.example.toml config.toml
   ```
   Edit `config.toml` to set your BUSY Bar address and integration-specific settings.

4. Test an integration with dry-run:
   ```bash
   cd integrations
   uv run python -m calendar_countdown.main --once --dry-run
   ```
   Or for CI status:
   ```bash
   uv run python -m ci_status.main --once --dry-run
   ```

## How it works

The display is a shared 72×16 canvas. Each integration publishes text, shapes, or status via the `busybar.client.BusyBarClient` API (see [`src/busybar/client.py`](src/busybar/client.py)). The display arbitrates by **priority**:

- **Built-in apps** sit at priority **10** — the idle baseline.
- **Integrations** claim higher priorities to preempt the baseline: `calendar_countdown` at **20**, CI alerts at **60**.
- **Active BUSY session** at **90** — an authenticated BUSY firmware state that outranks all integrations (e.g., CI failures blink the LED during an active session).
- **Equal-or-higher priority wins** the display. Each element has an optional `timeout`; if the source doesn't refresh within that window, the element self-clears.

The `application_name` field tags the source, letting the display track ownership and multi-instance behavior.

## Adding an integration

To add a new integration:

1. Read [`src/busybar/client.py`](src/busybar/client.py) — the public API for drawing to the display.
2. Follow the **logic/adapter split**:
   - **Logic module**: your domain (e.g., polling a calendar or API, computing state).
   - **Adapter module** (`main.py`): connects logic to the BusyBar display, handles CLI args, and lifecycle.
3. Add per-integration docs to your `README.md` — document config options, API tokens, and any platform-specific setup (e.g., macOS Calendar permission prompts).

## Configuration

`config.toml` is **not checked in** (it's in `.gitignore`). This repo follows a **no-secrets-by-construction** policy:

- All secrets (API tokens, credentials) go in `config.toml`, which you provide locally.
- The repo ships only `config.example.toml`, documenting all fields and defaults.
- CI/CD can inject secrets via environment-variable expansion in config parsing if needed.

## Cloud transport

By default, `BusyBarClient` talks to your BUSY Bar directly over the LAN
(`[device].host`). As of v1.6, it can automatically fall back to BUSY's
cloud relay if the local device becomes unreachable — USB unplugged,
Wi-Fi drop, the device off — and recover back to local on its own once
it's reachable again. This is entirely optional and off by default.

### Setting it up

1. Create a token at [cloud.busy.app](https://cloud.busy.app) → **API
  tokens** tab → create a new token with the **"BUSY Bar"** scope. This
  scope grants full control of exactly one linked device — there's no
  separate device ID to configure; the token itself identifies which
  device it talks to.
2. Add it to your `config.toml` (**never** `config.example.toml`, and
  never anything committed to the repo — see "Configuration" above):
   ```toml
   [device]
   host = "10.0.4.20"
   cloud_token = "paste-your-real-token-here"
   ```
3. Optionally set `transport` (default `"auto"`):
   - `"auto"` — local first, cloud fallback when the local device is
     unreachable and `cloud_token` is set. Recovers back to local
     automatically.
   - `"local"` — local only, never falls back (identical to pre-v1.6
     behavior; the default if you never set `cloud_token`).
   - `"cloud"` — forced cloud only, never attempts local. Mainly useful
     for deliberately exercising/debugging the cloud path.
4. `cloud_base_url` defaults to `https://api.busy.app/busybar` and
   normally doesn't need to change — see the base-URL note below.

### Rotating or revoking a token

Manage tokens from the same **API tokens** tab on cloud.busy.app.
Revoking a token takes effect **immediately and cannot be undone** — if
you're rotating, create and deploy the replacement token first, then
revoke the old one, rather than revoking first.

### What cloud fallback does NOT cover

Continuous status streaming (`/api/status/ws`) is local-only by design —
the cloud API has no equivalent, so a caller relying on the status
WebSocket will not get a cloud fallback for it. Everything else this
client uses (`draw`, `clear`, `status`, `get_busy`, `set_busy_simple`,
`play_audio`) is a synchronous request/response call and mirrors 1:1
over cloud.

### Post-merge live-probe checklist

This round's tests are entirely mocked — no cloud request has been made
against a real token, since the operator hadn't provisioned one yet.
**Before relying on cloud fallback in practice**, run this checklist
once a real `cloud_token` is in `config.toml`:

1. **Forced-cloud draw probe.** Set `transport = "cloud"` temporarily
  and run a `client.draw(...)` (e.g. via either integration's
  `--once --dry-run=false` path, or a one-off script) to confirm the
  token is valid and a real device draw round-trips over the cloud
  relay end to end.
2. **Cadence headroom check.** No rate limit or cadence guidance is
  documented anywhere for the cloud API (see
  `scratchpad/busy-cloud-api-research.md`'s "Open items"). Run
  `calendar_countdown` (10s ambient redraw cadence) forced onto cloud
  transport for a few minutes and confirm no throttling/errors show up
  before trusting cloud fallback to hold up under sustained polling.
3. **Base-URL ambiguity.** This codebase defaults `cloud_base_url` to
  `https://api.busy.app/busybar`, but busylib-py's own hardcoded default
  is the differently-hosted `https://proxy.busy.app` — an unresolved
  discrepancy in the source research, not something this round's mocked
  tests can settle. Confirm which base actually works against a live
  token (or whether both do) and update the default/docs here if
  `api.busy.app/busybar` turns out to be wrong or non-canonical.

Set `transport` back to `"auto"` (or leave it, since `"auto"` is the
default) once the checklist above passes.

## What's inside

| Integration | Description |
|---|---|
| [`calendar_countdown`](integrations/calendar_countdown/) | Next-meeting countdown on the LED display from macOS Calendar; optional auto-BUSY during events. |
| [`ci_status`](integrations/ci_status/) | GitHub Actions status via REST with ETag caching; red alert on failure, yellow on stuck-queued runs. |
