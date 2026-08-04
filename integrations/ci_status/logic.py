import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# Reuse the calendar integration's generic text/formatting helpers rather
# than reimplementing them: ascii_safe (device text sanitization),
# _format_countdown (minutes-granular "~4m" / "~1h05m" formatting, reused
# verbatim by the running badge's ETA text per the v1.5 spec), _title_fits
# (scroll-vs-static decision, parameterized by width so it's not
# calendar-geometry-specific), SCROLL_RATE/SCROLL_DELAY_MS (the same scroll
# timing), and PANEL_WIDTH/PANEL_HEIGHT (hardware constants, not actually
# calendar-specific despite living in that module). Geometry and palette
# below are ci_status's own -- only the algorithms are shared, not the
# layout constants, since the two integrations' layouts are visually
# similar (v1.4 "airy" language) but structurally distinct.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from calendar_countdown.logic import (  # noqa: E402
    ascii_safe, _format_countdown, _title_fits,
    SCROLL_RATE, SCROLL_DELAY_MS, PANEL_WIDTH, PANEL_HEIGHT,
)

FAILING = {"failure", "timed_out", "startup_failure"}


@dataclass
class RepoState:
    repo: str
    failing: list[str]
    stuck: list[str]


@dataclass
class RunningInfo:
    """Everything build_ci_payload needs to render the running badge,
    pre-computed by the caller (main.run_once) so the precedence/render
    logic here stays pure and testable without network mocking."""
    run: dict
    repo: str
    other_count: int
    median_minutes: float | None
    now: datetime


def _parse_ts(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def evaluate_runs(repo: str, runs: list[dict], now: datetime,
                  stale_queued_minutes: int) -> RepoState:
    latest: dict[int, dict] = {}
    for r in runs:  # API returns newest first; keep first seen per workflow
        latest.setdefault(r["workflow_id"], r)
    failing, stuck = [], []
    for r in latest.values():
        if r.get("conclusion") in FAILING:
            failing.append(r["name"])
        elif r.get("status") == "queued" and stale_queued_minutes > 0:
            age_min = (now - _parse_ts(r["created_at"])).total_seconds() / 60
            if age_min >= stale_queued_minutes:
                stuck.append(r["name"])
    return RepoState(repo=repo, failing=sorted(failing), stuck=sorted(stuck))


# --- running-job badge (v1.5) --------------------------------------------------
#
# Firmware priority-arbitration finding that shapes this whole section
# (empirical probe, see the implementation report and spec doc's v1.5
# section for the full write-up): the OpenAPI doc claims "equal-priority
# requests from a different application_name override whatever is on
# screen," but probing found this is FALSE on firmware 1.1.1 -- a
# different-application_name draw at the SAME priority as the currently
# showing app is REJECTED (409 "low priority"); only a STRICTLY GREATER
# priority succeeds. Since calendar_countdown draws continuously at
# priority 20, the running badge must use priority 21, not 20 as the task
# brief literally specified, or every running-badge draw attempt would
# 409 whenever the calendar happens to be the currently-showing app (which
# is most of the time). This is a deliberate, evidence-backed deviation
# from the brief's literal instruction.
#
# Second finding: occluded elements do NOT reappear once the occluder's
# elements expire or are cleared -- they are evicted, not merely hidden.
# Confirmed by probing both the timeout-expiry and explicit-clear cases (a
# lower-priority app's still-live element stayed gone, screen went black,
# in both). This means the "clean alternation, zero coordination" path in
# the brief does not apply; the badge and the calendar do not smoothly
# swap -- the badge occupies the screen for its own timeout, then the
# screen goes blank until *some* app performs a fresh draw (either the
# badge's own next cycle, or the calendar's independent 60s redraw landing
# in the gap by chance). See the report for the actually-observed rhythm.
RUNNING_PRIORITY = 21
RUNNING_BADGE_TIMEOUT_S = 10  # fixed per the brief ("timeout ~10s"), independent
                              # of running_poll_seconds's configured value

# Palette: a distinct cyan/blue "running" theme, following the same
# luminance-contrast principle established in the v1.4 calendar work (near-
# black surface + bright saturated text; no mid-luminance "dim-mud" fills).
# The track's own groove color is a decorative element, not a text-bearing
# surface, so it's allowed to be a touch brighter than the panel background
# without violating that rule -- same treatment as the calendar's TRACK_COLOR.
RUNNING_BG_GRADIENT = ["#031A2EFF", "#00060DFF"]
RUNNING_TITLE_COLOR = "#7FDBFFFF"
RUNNING_TRACK_COLOR = "#0F2A42FF"
RUNNING_TRACK_FILL_COLOR = "#29B6F6FF"   # spec: "solid cyan" -- one flat color, no gradient
RUNNING_NUMERAL_COLOR = "#66E1FFFF"

# Geometry: same v1.4 "airy" row template as the calendar (title ribbon at
# the top, blank buffer row, full-width horizon-line track, numeral row
# below) but independently declared here -- these are ci_status's own rows,
# not a shared layout object, even though the values happen to match.
RUNNING_TITLE_X = 2
RUNNING_TITLE_Y = -2      # ink rows 0-4 (small font); see calendar_countdown.logic
                          # for the underlying +2px text ink-offset model this assumes
RUNNING_TITLE_WIDTH = 68  # 2px margin each side of a 72px panel
RUNNING_TRACK_Y = 6       # row 5 is a deliberate blank buffer, same as v1.4 calendar
RUNNING_TRACK_HEIGHT = 1
RUNNING_NUMERAL_X = 2
RUNNING_NUMERAL_Y = 5     # ink rows 7-15 (large font, 9px)


def _pr_or_branch(run: dict) -> str:
    """PR number ("#42") if the run belongs to a pull request, else the
    branch it ran on (fork/push-triggered runs have an empty
    pull_requests array)."""
    prs = run.get("pull_requests") or []
    if prs:
        return f"#{prs[0]['number']}"
    return run.get("head_branch") or ""


def select_running_run(running_by_repo: dict[str, list[dict]]) -> tuple[dict, str, int] | None:
    """Pick the most-recently-started in_progress run across every
    configured repo. Returns (run, repo, other_running_count) or None if
    nothing is running. `other_running_count` is every other currently-
    running run (any repo, any workflow) besides the selected one -- the
    "+N" the title badge shows."""
    candidates = [(r, repo) for repo, runs in running_by_repo.items()
                 for r in runs if r.get("status") == "in_progress"]
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0].get("run_started_at", ""), reverse=True)
    best_run, best_repo = candidates[0]
    return best_run, best_repo, len(candidates) - 1


def compute_median_duration_minutes(successful_runs: list[dict]) -> float | None:
    """Median of (updated_at - run_started_at) in minutes across up to the
    first 5 runs given (the caller already requests per_page=5, but this
    stays defensive rather than trusting that). None if no run has both
    timestamps -- "no history", not an error."""
    durations = []
    for r in successful_runs[:5]:
        started, updated = r.get("run_started_at"), r.get("updated_at")
        if not started or not updated:
            continue
        durations.append((_parse_ts(updated) - _parse_ts(started)).total_seconds() / 60)
    if not durations:
        return None
    durations.sort()
    n = len(durations)
    mid = n // 2
    if n % 2 == 1:
        return durations[mid]
    return (durations[mid - 1] + durations[mid]) / 2


def _elapsed_minutes(run: dict, now: datetime) -> float:
    return (now - _parse_ts(run["run_started_at"])).total_seconds() / 60


def _format_eta_text(run: dict, median_minutes: float | None, now: datetime) -> str:
    """`~4m` / `~1h05m` (reuses _format_countdown verbatim, tilde-prefixed
    to mark it as an estimate) when history exists; `soon` once the
    estimate floors to under a minute remaining (covers both "exactly at
    or past the median" and "a few seconds under a minute left" -- neither
    reads sensibly as "~0m"); `<elapsed> in` (e.g. "3m in") when there's no
    history to estimate from at all.
    """
    elapsed = _elapsed_minutes(run, now)
    if median_minutes is None:
        return f"{_format_countdown(elapsed)} in"
    eta = max(0.0, median_minutes - elapsed)   # spec: "floored at 0"
    if int(eta) <= 0:
        return "soon"
    return f"~{_format_countdown(eta)}"


def _progress_width(elapsed_minutes: float, median_minutes: float | None) -> int:
    """Track-fill width in px: elapsed/median of the panel width, clamped
    to [1, PANEL_WIDTH]. Full width when the ratio is unknown (no history)
    or the run has overrun its median -- same defensive shape as the
    calendar's _track_fill_width (full-on-bad-denominator rather than
    raising or dividing by zero)."""
    if median_minutes is None or median_minutes <= 0:
        return PANEL_WIDTH
    if elapsed_minutes <= 0:
        return 1
    width = round(PANEL_WIDTH * elapsed_minutes / median_minutes)
    return max(1, min(PANEL_WIDTH, width))


def _build_running_title(run: dict, repo: str, other_count: int) -> str:
    """"REPO #PR WORKFLOW" (or "REPO branch-name WORKFLOW" for fork/push
    runs), with a "+N" suffix when other runs are also active."""
    ref = _pr_or_branch(run)
    workflow = run.get("name") or ""
    parts = [p for p in (repo, ref, workflow) if p]
    text = " ".join(parts)
    if other_count > 0:
        text = f"{text} +{other_count}"
    return ascii_safe(text).upper()


def _build_running_elements(info: RunningInfo, timeout_s: int) -> list[dict]:
    """v1.4-language badge: gradient bg, title ribbon (scrolls if it
    doesn't fit), full-width horizon-line track repurposed as elapsed/
    median progress, and a large ETA numeral -- no card surfaces, no
    native countdown element, same design lineage as the v1.4 calendar
    layout. Draw order is z-order, first = behind.
    """
    title_text = _build_running_title(info.run, info.repo, info.other_count)
    eta_text = _format_eta_text(info.run, info.median_minutes, info.now)
    elapsed = _elapsed_minutes(info.run, info.now)
    track_width = _progress_width(elapsed, info.median_minutes)

    bg_element = {
        "id": "bg", "type": "rectangle", "x": 0, "y": 0,
        "width": PANEL_WIDTH, "height": PANEL_HEIGHT,
        "fill": "gradient_v", "fill_colors": RUNNING_BG_GRADIENT,
        "border_width": 0, "timeout": timeout_s,
    }
    title_element = {
        "id": "title", "type": "text", "text": title_text, "font": "small",
        "color": RUNNING_TITLE_COLOR, "x": RUNNING_TITLE_X, "y": RUNNING_TITLE_Y,
        "width": RUNNING_TITLE_WIDTH, "timeout": timeout_s,
    }
    if not _title_fits(title_text, RUNNING_TITLE_WIDTH):
        title_element.update({
            "scroll_rate": SCROLL_RATE,
            "scroll_start_delay": SCROLL_DELAY_MS,
            "scroll_repeat_delay": SCROLL_DELAY_MS,
        })
    track_element = {
        "id": "track", "type": "rectangle", "x": 0, "y": RUNNING_TRACK_Y,
        "width": PANEL_WIDTH, "height": RUNNING_TRACK_HEIGHT, "fill": "solid",
        "fill_colors": [RUNNING_TRACK_COLOR], "border_width": 0, "timeout": timeout_s,
    }
    track_fill_element = {
        "id": "track_fill", "type": "rectangle", "x": 0, "y": RUNNING_TRACK_Y,
        "width": track_width, "height": RUNNING_TRACK_HEIGHT, "fill": "solid",
        "fill_colors": [RUNNING_TRACK_FILL_COLOR], "border_width": 0, "timeout": timeout_s,
    }
    numeral_element = {
        "id": "eta", "type": "text", "text": eta_text, "font": "large",
        "color": RUNNING_NUMERAL_COLOR, "x": RUNNING_NUMERAL_X, "y": RUNNING_NUMERAL_Y,
        "timeout": timeout_s,
    }
    return [bg_element, title_element, track_element, track_fill_element, numeral_element]


def _text_element(text: str, color: str, timeout_s: int, font: str = "normal") -> dict:
    return {"id": "ci", "type": "text", "text": text, "font": font,
            "x": 0, "y": 4, "width": 72, "color": color,
            "scroll_rate": 2000, "scroll_start_delay": 1000,
            "scroll_repeat_delay": 2000, "timeout": timeout_s}


def _badge_elements(text: str, bg_color: str, text_color: str, timeout_s: int) -> list[dict]:
    """Full-panel rounded-rect background + bold scrolling text over it."""
    # border_width=0: RectangleElement defaults to a 1px white border, which
    # would draw an unwanted white outline around the badge (verified on-device).
    bg = {"id": "bg", "type": "rectangle", "x": 0, "y": 0, "width": 72, "height": 16,
          "radius": 2, "fill": "solid", "fill_colors": [bg_color], "border_width": 0,
          "timeout": timeout_s}
    return [bg, _text_element(text, text_color, timeout_s, font="bold")]


def build_ci_payload(states: list[RepoState], show_green: bool, timeout_s: int,
                     running: RunningInfo | None = None) -> dict | None:
    """Precedence: failure > stuck > running > quiet green > nothing.
    Failure and stuck stay at priority 60 (unchanged) and are evaluated
    first specifically so they always win over a running badge even if
    both conditions are true in the same poll -- an active alert must never
    be preempted by "just" a running-job status update. `running`, when
    given, is a fully pre-computed RunningInfo (see its docstring for why
    the render logic here takes it pre-computed rather than raw run data).
    """
    failures = [(s.repo, name) for s in states for name in s.failing]
    stuck = [(s.repo, name) for s in states for name in s.stuck]
    if failures:
        text = "CI FAIL " + " ".join(f"{repo}:{name}" for repo, name in failures)
        return {"elements": _badge_elements(text, "#A32D2DFF", "#FFFFFFFF", timeout_s),
                "priority": 60, "led": "#FF0000FF"}
    if stuck:
        text = "CI stuck " + " ".join(f"{repo}:{name}" for repo, name in stuck)
        return {"elements": _badge_elements(text, "#BA7517FF", "#0B0B0BFF", timeout_s),
                "priority": 60, "led": None}
    if running is not None:
        # RUNNING_BADGE_TIMEOUT_S (fixed ~10s), not the caller's timeout_s
        # (derived from poll_seconds) -- the running badge's on-screen
        # duration is a design constant of the alternation cadence, not a
        # function of how often ci_status happens to poll.
        return {"elements": _build_running_elements(running, RUNNING_BADGE_TIMEOUT_S),
                "priority": RUNNING_PRIORITY, "led": None}
    if show_green:
        return {"elements": [_text_element("CI ok", "#00FF00FF", timeout_s)],
                "priority": 60, "led": None}
    return None
