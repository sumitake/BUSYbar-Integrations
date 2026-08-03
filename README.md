# BUSY Bar Integrations

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

## What's inside

| Integration | Description |
|---|---|
| [`calendar_countdown`](integrations/calendar_countdown/) | Next-meeting countdown on the LED display from macOS Calendar; optional auto-BUSY during events. |
| [`ci_status`](integrations/ci_status/) | GitHub Actions status via REST with ETag caching; red alert on failure, yellow on stuck-queued runs. |
