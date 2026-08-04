"""Shared display priority ladder and dwell/redraw contracts for every
busybar integration.

## Two firmware facts, established empirically (not from the device's own
## OpenAPI documentation, which is wrong about the first one) during the
## v1.5 running-CI badge work. See the spec doc's "Probe findings" section
## (`docs/superpowers/specs/2026-08-03-calendar-ci-integrations-design.md`,
## v1.5) for the full probe transcripts.

1. **Equal priority from a different `application_name` is REJECTED, not
   an override.** The OpenAPI doc claims "equal-priority requests from a
   different application_name override whatever is on screen" -- probed
   directly against a live app at priority 20 and found every equal- or
   lower-priority draw from a different app_name returns `409 {"error":
   "Not drawn due to low priority"}`. Only a STRICTLY GREATER priority
   succeeds. Any two tiers that need to take turns on screen must be on
   different priority numbers, not the same one.

2. **Occluded elements are EVICTED, not restored.** When a higher-priority
   app's elements expire (their own `timeout`) or are explicitly cleared,
   a lower-priority app's still-live elements do NOT reappear -- the panel
   goes black and stays black until *some* app performs a fresh draw.
   Probed for both the timeout-expiry and explicit-clear cases; both
   evict. There is no cross-process coordination between integrations (each
   is an independent poller), so an ambient app can only "win back" the
   screen by drawing again on its own schedule and getting lucky with the
   timing -- it is never handed the screen back automatically.

## The ladder

Every integration draws under one of these tiers. Pick the tier that
matches your update pattern; don't invent a new priority number.
"""

PRIORITY_AMBIENT = 20
"""Persistent baseline apps (e.g. calendar_countdown). Contract: redraw at
least every `AMBIENT_REDRAW_SECONDS` seconds, with each element's `timeout`
set via `ambient_timeout(poll_seconds)` (1.5x the poll interval, so a dead
poller's display self-clears rather than sticking). Must tolerate being
evicted at any time by a higher-priority draw (fact 2 above) -- there is no
"resume where I left off"; the next scheduled redraw is the only way back
on screen. `AMBIENT_REDRAW_SECONDS` exists specifically so an overlay
tier's dwell gaps (see PRIORITY_OVERLAY) are short enough for an ambient
app to realistically land one of its redraws inside them.

Tuning history (v1.5): started at 15s against a 10s overlay dwell gap;
on-device re-measurement (130s window, 6 dwell cycles) showed the ambient
app only recovering 2 of 6 gaps -- dark gaps were still ~10s in the other
4, exceeding the "~5s" target. Dropped to 10s (matching the dwell gap
exactly, so an ambient redraw firing anywhere in the gap window has a much
better chance of landing inside it) and re-measured; see the spec doc's
v1.5 section for both rounds' verbatim results.
"""
AMBIENT_REDRAW_SECONDS = 10


def ambient_timeout(poll_seconds: float) -> int:
    """Element timeout (seconds) for an ambient-tier draw at the given poll
    interval: 1.5x poll, floored to an int (matches the existing
    calendar_countdown convention)."""
    return int(poll_seconds * 1.5)


PRIORITY_OVERLAY = 21
"""Short-dwell time-shared overlays (e.g. the running-CI badge). Must be
strictly greater than PRIORITY_AMBIENT (fact 1 above) -- equal priority
against a different app_name is rejected, not a hand-off. Contract: draw
with element `timeout` = `OVERLAY_DWELL_SECONDS`, then stay silent (no
draw, no clear) for at least one more dwell period before redrawing again,
so an ambient app's own redraw has a real chance to land in the gap (fact
2 above means the ambient app is never automatically restored -- it can
only reclaim the screen with its own fresh draw). Use `overlay_gap_elapsed`
to decide whether enough silence has passed.
"""
OVERLAY_DWELL_SECONDS = 10


def overlay_gap_elapsed(last_dwell_end, now) -> float:
    """Seconds elapsed since an overlay's last dwell ended. `last_dwell_end`
    is `None` (never drawn yet) treated as infinitely long ago, so the
    first draw is never gated. Callers redraw when this returns
    `>= OVERLAY_DWELL_SECONDS`.
    """
    if last_dwell_end is None:
        return float("inf")
    return (now - last_dwell_end).total_seconds()


PRIORITY_ALERT = 60
"""Urgent, preempting states (e.g. CI failure/stuck badges). Always wins
over PRIORITY_AMBIENT and PRIORITY_OVERLAY by virtue of being a strictly
higher number (fact 1 above) -- no dwell/silence contract; draw
immediately and keep redrawing every poll while the condition holds.
"""

PRIORITY_SESSION = 90
"""Reference only -- the firmware's own BUSY/CUSTOM work-session tier.
No integration in this repo draws at this priority; it's documented here
so the full ladder (including the firmware-owned ceiling) is visible in
one place.
"""
