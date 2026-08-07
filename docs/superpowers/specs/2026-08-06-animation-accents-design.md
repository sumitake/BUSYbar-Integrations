# Stock-Animation Accents for calendar_countdown + ci_status — Design Spec

**Date:** 2026-08-06
**Status:** approved design; on-device composition spikes resolved (§7)
**Depends on:** existing `calendar_countdown` and `ci_status` integrations, `busybar.display` priority ladder, `AnimationElement` (established during nyan_filler work: `2026-08-06-nyan-filler-design.md` §5b).

---

## 1. Goal

Bring the device's built-in stock animations into the two existing integrations to make the important moments more alive:

- **Calendar** — animated calendar icons accenting the pre-start escalation (5-min and 1-min), and a full-panel animation takeover for the **first 60 s after an event begins** (aligned with the T-0 chirp) as a strong "it's starting / you're running late" alarm.
- **CI** — a small animated "working" spinner on the running badge.

## 2. Scope & non-goals

**In scope:** edits to `calendar_countdown/logic.py` (+ `main.py`/`select_priority` for the start window), `ci_status/logic.py` (running badge only), config additions, agent restarts.

**Non-goals:**
- **No quota animations** — the quota frames are %-gauges with no thematic stock animation; left unchanged (operator's choice).
- **No new integration, no new assets** — stock animations are referenced in place via `stock_path`; nothing is uploaded or bundled.
- No change to the notice (15-min/amber) stage, the approach window, or the CI alert/quiet-green/quota displays.

## 3. Stock animations used

Referenced by `stock_path` (form **`shared/<name>.anim`**, verified working — §7). All are device stock at `/ext/apps_assets/shared/animations/`:

| Use | Animation | Size |
|---|---|---|
| Calendar 5-min accent | `calendar_event_16x16` | 16×16 |
| Calendar 1-min accent | `calendar_reminder_16x16` (calendar + bell) | 16×16 |
| Calendar start takeover (configurable) | `meeting_72x16` (default) | 72×16 |
| CI running spinner | `spinner_front_8x8` | 8×8 |

The full-panel stock graphics **bake in their own word** ("MEETING", "BOOKED", …) and light ~97 % of the panel, so the start takeover is a genuine full-screen replacement showing that fixed word for every event (a documented tradeoff; configurable per §6).

## 4. Calendar — three animation moments

`build_elements` (and `select_priority`) gain the following. All draws remain a **single** `client.draw` of mixed elements (composition verified §7); the animation is one more element layered by draw order.

### 4a. Pre-start accents (countdown preserved)

| Stage | Trigger | Elements |
|---|---|---|
| **Warn** | `minutes_left ≤ warn_minutes` (5) and `> imminent_minutes` (1) | existing red `bg` + drain `track` + **`calendar_event_16x16` icon at (x=0, y=0)** + title shifted to `x=18` (reduced width, scrolls) + large countdown at existing `CD_TEXT_X=39` |
| **Imminent** | `minutes_left ≤ imminent_minutes` (1), not yet started | red `bg` + **`calendar_reminder_16x16` icon at (x=0, y=0)** + **title dropped** + large red countdown at `CD_TEXT_X=39` + LED blink (existing) |

Priority unchanged: both are `PRIORITY_AMBIENT_URGENT` (65) exactly as today. Element-id set changes (adds `cal_icon`); the existing transition-clear logic already clears on id-set change.

Layout: icon occupies `x=0..15`; the countdown numeral keeps its current right-side position (`x=39`), so the icon and number never overlap. At warn, the title lives in the `x=18..38` gap and scrolls; at imminent the title is dropped so the icon + big number own the panel.

### 4b. Start takeover (new — first 60 s after start)

A new window `just_started` = `in_progress` **and** `elapsed_since_start < start_window_seconds` (default 60).

- **Display:** a single full-panel `AnimationElement` (`stock_path` = the configured `start_animation`, default `meeting_72x16`, `x=0, y=0, loop=true`) — replaces the normal in-progress "ENDS" display for this window.
- **Priority:** held at `PRIORITY_AMBIENT_URGENT` (65) for the whole window — so it preempts the filler/ambient and reads as an alarm, matching the T-0 chirp. `select_priority` returns 65 when `just_started`, else the existing behavior (in-progress → `PRIORITY_AMBIENT` 20).
- **After the window:** reverts to the normal in-progress "ENDS" display at `PRIORITY_AMBIENT` (20). The id-set change (full-panel `cal_start_anim` → `ends`/`time` set) triggers the existing transition-clear.
- **Disabled** (`start_animation = ""`): the window is skipped entirely — in-progress behaves exactly as today from T-0.

The chirp (T-0, existing) and this takeover are independent but coincide by construction; no coupling between them beyond both keying off event start.

## 5. CI — running-badge spinner

In `_build_running_elements` only (never the quota frames):

- Add `spinner_front_8x8` at **(x=64, y=0)** (top-right corner), `loop=true`.
- **Reserve its corner:** reduce the running title's available width from `RUNNING_TITLE_WIDTH` (68) to **60** so the scrolling title never runs under the spinner. The ETA numeral/label stay bottom-left, far from the corner.
- Adds `run_spinner` to the badge's element-id set; the unified shape-tracker in `ci_status/main.py` already clears on any shape change, so the badge↔quota↔alert seams stay correct.

## 6. Config

```toml
[calendar_countdown]
escalation_icons = true          # the 5-min / 1-min animated calendar icons
start_animation = "meeting_72x16"  # full-panel takeover for the first minute after start; "" disables
start_window_seconds = 60        # how long the start takeover holds (and its urgent-priority window)

[ci_status]
running_spinner = true           # animated 8x8 spinner on the running badge
```

All default **on** (operator wants them). `escalation_icons = false` reverts the pre-start stages to today's text-only display; `start_animation = ""` skips the takeover; `running_spinner = false` reverts the badge.

## 7. Spike results (2026-08-06, live device, fw 1.1.1)

- **stock_path referencing works.** `AnimationElement` with `stock_path: "shared/<name>.anim"` renders (HTTP 200) for every candidate — also `shared/animations/<name>.anim` and `animations/<name>.anim`; the bare filename 400s. → no asset bundling; reference stock in place.
- **AnimationElement composes with text + rectangles in one draw.** A single draw of `bg` rect + `title`/countdown text + a 16×16 animation icon rendered all three, layered by draw order, with the animation self-looping — the icon-accent approach is sound. Captured proposed layouts for calendar 5-min, calendar 1-min, and the CI running badge with the spinner in two corner positions.
- **Legibility.** The colorful 16×16 calendar icons read clearly beside the amber/red countdown; no clash. The 8×8 spinner fits the badge's top-right corner without touching the ETA numeral (title width reserved).
- **Full-panel takeover renders** at the urgent tier (established: a >current-priority draw wins; `meeting_72x16` lights ~1130/1152 px).

## 8. Testing

Pure-logic unit tests (`calendar_countdown` and `ci_status` test files):
- Calendar stage/window → element-set selection: warn adds `cal_icon`=event; imminent adds `cal_icon`=reminder and drops the title; `just_started` (in-progress, elapsed < window) returns the single full-panel `cal_start_anim` at priority 65; in-progress past the window reverts to the "ENDS" set at 20; `start_animation=""` skips the window.
- `select_priority`: `just_started` → 65; other in-progress → 20 (unchanged).
- `escalation_icons=false` / `running_spinner=false` produce today's element sets exactly (regression guard).
- CI running badge: `run_spinner` present when `running_spinner` true; title width is 60 when the spinner is on, 68 when off; quota frames never gain a spinner.

On-device verification (operator/primary pass): the three calendar moments across a real event's escalation + start; the CI spinner during a real run; each animation composes/reverts cleanly; agents restarted.

## 9. Open questions / risks

- **Baked-word takeover:** the default `meeting_72x16` shows "MEETING" for every event start (fine for a work calendar; configurable). No per-event-type mapping (we don't have event-type data) — accepted.
- **Redraw cadence during the takeover:** the calendar polls every `poll_seconds` (10) and shortens near T-0; the animation self-loops on-device between redraws (redraw-continues, established), so the 60 s window animates smoothly with the app re-asserting each poll.
- **Icon vs. title space at warn:** the title scrolls in the narrow `x=18..38` band; if it reads cramped on-device, the fallback is to drop the title at warn too (icon + countdown only) — decided during on-device verification.

## 10. Rollout

New branch off `main`; SDD; PR (public, PR-gated). After merge, restart `com.busybar.calendar-countdown` and `com.busybar.ci-status` (the plists are unchanged; a `launchctl kickstart -k` picks up the new code).
