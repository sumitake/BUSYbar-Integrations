from datetime import datetime, timedelta, timezone

from busybar.display import (
    PRIORITY_AMBIENT, PRIORITY_OVERLAY, PRIORITY_AMBIENT_RAISED, PRIORITY_ALERT,
    PRIORITY_AMBIENT_URGENT, PRIORITY_SESSION,
    AMBIENT_REDRAW_SECONDS, OVERLAY_DWELL_SECONDS,
    ambient_timeout, overlay_gap_elapsed,
)

NOW = datetime(2026, 8, 3, 13, 37, tzinfo=timezone.utc)


def test_priority_ladder_values():
    assert PRIORITY_AMBIENT == 20
    assert PRIORITY_OVERLAY == 21
    assert PRIORITY_AMBIENT_RAISED == 25
    assert PRIORITY_ALERT == 60
    assert PRIORITY_AMBIENT_URGENT == 65
    assert PRIORITY_SESSION == 90

def test_priority_ladder_is_strictly_increasing():
    # Load-bearing: equal priority from a different application_name is
    # REJECTED by the firmware (probed, contradicts the OpenAPI doc), so
    # every tier that must be able to preempt the one below it needs a
    # strictly greater number, not merely a "greater or equal" one.
    # v1.5.2: 20 < 21 < 25 < 60 < 65 < 90.
    ladder = [PRIORITY_AMBIENT, PRIORITY_OVERLAY, PRIORITY_AMBIENT_RAISED,
             PRIORITY_ALERT, PRIORITY_AMBIENT_URGENT, PRIORITY_SESSION]
    assert ladder == sorted(set(ladder))
    assert len(ladder) == len(set(ladder))

def test_overlay_priority_strictly_exceeds_ambient():
    assert PRIORITY_OVERLAY > PRIORITY_AMBIENT

def test_ambient_raised_strictly_between_overlay_and_alert():
    assert PRIORITY_OVERLAY < PRIORITY_AMBIENT_RAISED < PRIORITY_ALERT

def test_ambient_urgent_strictly_between_alert_and_session():
    assert PRIORITY_ALERT < PRIORITY_AMBIENT_URGENT < PRIORITY_SESSION

def test_cadence_constants():
    # Tuned down from 15 to 10 after on-device re-measurement showed 15s
    # only recovering the ambient app's screen time in 2 of 6 dwell cycles
    # -- see busybar/display.py's AMBIENT_REDRAW_SECONDS docstring.
    assert AMBIENT_REDRAW_SECONDS == 10
    assert OVERLAY_DWELL_SECONDS == 10


# --- ambient_timeout -----------------------------------------------------------

def test_ambient_timeout_is_1_5x_poll():
    assert ambient_timeout(15) == 22   # int(15 * 1.5) == 22 (floors 22.5)
    assert ambient_timeout(60) == 90
    assert ambient_timeout(10) == 15

def test_ambient_timeout_floors_not_rounds():
    assert ambient_timeout(11) == 16   # 16.5 floors to 16, not rounds to 17


# --- overlay_gap_elapsed ---------------------------------------------------------

def test_overlay_gap_elapsed_infinite_when_never_drawn():
    assert overlay_gap_elapsed(None, NOW) == float("inf")

def test_overlay_gap_elapsed_computes_seconds_since_dwell_end():
    last_end = NOW - timedelta(seconds=12)
    assert overlay_gap_elapsed(last_end, NOW) == 12.0

def test_overlay_gap_elapsed_zero_immediately_after_dwell_end():
    assert overlay_gap_elapsed(NOW, NOW) == 0.0

def test_overlay_gap_elapsed_matches_dwell_threshold_semantics():
    # The gate callers use is `>= OVERLAY_DWELL_SECONDS`; sanity-check the
    # boundary value itself is exact, not off-by-a-rounding-error.
    last_end = NOW - timedelta(seconds=OVERLAY_DWELL_SECONDS)
    assert overlay_gap_elapsed(last_end, NOW) == OVERLAY_DWELL_SECONDS


def test_filler_priority_below_builtin_and_ambient():
    from busybar.display import PRIORITY_FILLER
    assert PRIORITY_FILLER == 5
    assert 0 < PRIORITY_FILLER < 10 < PRIORITY_AMBIENT  # 10 = built-in app tier
