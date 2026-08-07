# Nyan Cat Dark-Filler — Design Spec

**Date:** 2026-08-06
**Status:** approved design; all three on-device spikes resolved (see §5b)
**Depends on:** `busybar.display` priority ladder, `src/busybar/client.py`, the existing `ci_status` / `calendar_countdown` integrations (as peers, not as callers)

---

## 1. Goal

Keep the BUSY Bar's 72×16 panel visually alive instead of black during the dead
gaps that appear while `ci_status` rotates its overlay frames — by drawing a
looping **Nyan Cat** animation at a priority low enough that every real
integration preempts it, and only when the panel would otherwise be dark.

The animation itself runs **on the device** (via the firmware's native
`AnimationElement`); the host's only job is a lightweight arbitration loop that
re-asserts the frame when the panel goes dark and stays quiet otherwise.

## 2. Scope & non-goals

**In scope**
- A new, self-contained integration `integrations/nyan_filler/` (pure `logic.py`
  + thin `main.py` loop), mirroring the structure of the existing two.
- A new shared priority tier `PRIORITY_FILLER` in `busybar/display.py`.
- Its own launchd agent `com.busybar.nyan-filler`.
- A `[nyan_filler]` config section.
- A configurable **quiet-hours** window during which the filler stays dark.

**Non-goals**
- **Not CI-gated.** The filler is "fill whenever the panel is dark," not "fill
  only during CI runs." The user chose always-on-when-dark scope specifically
  because it needs *zero* coordination with `ci_status` (matching this
  codebase's "independent pollers, no cross-process coordination" principle),
  and because a low-priority filler already yields to everything automatically.
  During a CI run this produces exactly the requested CI-screen↔Nyan
  alternation as a side effect.
- **No on-device logic.** The *arbitration* loop cannot run on the device — see
  §4. Only the animation *playback* is on-device.
- **No new dependencies.** stdlib + the existing `BusyBarClient`.

## 3. Background: what the "blank screen" actually is

`ci_status` draws its overlay frames (running badge, GraphQL/REST quota) at
`PRIORITY_OVERLAY` (21) with a 10 s element `timeout`, then deliberately goes
**silent for ~10 s** so the ambient calendar can reclaim the screen in the gap.
But the firmware **evicts** occluded elements rather than restoring them, so a
gap is only filled if *some* app actively redraws into it. When there is no
imminent calendar event, nothing redraws, and the gap goes black.

This is measured and documented in `integrations/calendar_countdown/README.md`:
the calendar recovers roughly 3 of 4 gaps *when an event is pending*, and
effectively 0 of 4 when none is. That recurring black gap — alternating ~10 s
frame / ~10 s black — is the "blank screen." The filler's job is to occupy it.

## 4. Platform investigation: there is no on-device runtime for our logic

This was investigated directly (device at `10.0.4.20`, its `/openapi.yaml`, the
community app repo, and the vendor's no-code widget article) because it
determines whether any of this can avoid the host. Findings:

- **The local HTTP API is a remote-control surface.** Every endpoint was read:
  there is no app-install, app-run, or task-schedule endpoint. `/api/display/draw`
  is the built-in **"Canvas"** app's remote-draw channel. The `install_manifest_*`
  codes in the spec are the **firmware-update** state machine, not app install.
- **The community "apps" are host-side.** `maxswinkels/busybar-apps` (source of
  the Nyan reference) states its apps are *"host-side Python scripts… not
  applications installed on the device itself."* Its `busybar-manager` companion
  is a *local* manager that proxies to the device.
- **The no-code "widgets" feature is also host-driven.** The vendor article
  ("Make BUSY Bar Widgets Without Coding") describes AI-generated **Python host
  scripts** rendering over HTTP; widgets are static images/text, carry no
  conditional/scheduling logic, and **do not persist when the host disconnects.**
- **What *is* on-device: playback.** `AnimationElement` with `loop: true` loops
  autonomously on the device once drawn — this is the piece we exploit to keep
  host cost near zero.

**Conclusion:** animation playback is on-device; the arbitration loop
(draw-when-dark, quiet-hours) must run on a host. This is inherent to the
platform, not a limitation of the design. The eviction model reinforces it:
after a higher app clears, Nyan is evicted, not restored, so an external actor
must re-assert the draw — nothing on the device will.

## 5. Architecture

A third integration, structured exactly like the existing two:

```
integrations/nyan_filler/
  __init__.py
  logic.py        # pure: quiet-hours gate, redraw decision, asset paths
  main.py         # thin loop: poll, gate, draw/clear, backoff
  com.busybar.nyan-filler.plist
  README.md
assets/nyan/…     # the baked animation asset(s), uploaded to the device once
src/busybar/display.py   # + PRIORITY_FILLER
config.example.toml      # + [nyan_filler]
tests/test_nyan_logic.py
```

No coordination with `ci_status` or `calendar_countdown`. The filler draws low;
the existing priority ladder does all arbitration.

## 5b. Spike results (2026-08-06, live device, firmware 1.1.1)

All verified against the bar at `10.0.4.20` via `/api/display/draw` and
framebuffer reads (`/api/screen?display=0`, base64 72×16×3 BGR).

- **SPK-1 — native `.anim` works.** Uploaded a real 72×16 `.anim` to a test app
  and drew it as an `AnimationElement` → HTTP 200, full panel rendered. The
  `.anim` "bicycle0" container is compiled from a documented `.zip`
  (`frame_N.png` + `meta.json`) via busylib-py's converter (§7). Native
  on-device playback confirmed feasible; no per-frame host pushing.
- **SPK-2 — redraw continues (no restart).** With a 1 fps test animation, the
  frame showing immediately before a same-element redraw was still showing
  immediately after (loop position preserved, no jump to frame 0). → the filler
  may redraw every poll unconditionally with no stutter (§8).
- **SPK-3 — priority floor is 0 at rest.** Polled draws across ~40 s during a
  live CI run: when the panel was black (idle **and** CI silence gap), a `@1`
  draw succeeded **16/16**; while a CI overlay frame was up (≥20) every `@1`/`@11`
  draw was rejected `409`. → `PRIORITY_FILLER = 5` fills black gaps and is
  rejected by any built-in app (10), so it never overrides one (§6).
- **SPK-4 — Python `bicycle0` encoder works.** A Python port of `seq2anim.ts`
  (raw-frame path) parsed the reference `tracks.anim` header correctly
  (72×16, rgb888, fps 1, 4 frames, 1 section) and produced a 2-frame test
  `.anim` that uploaded, drew, and animated on-panel as RED/BLUE alternating —
  correct BGR order and header. Custom-`.anim` generation is validated (§7).

## 6. Priority & arbitration

The device documents its own system-app priority tiers (from `/openapi.yaml`,
`priority` field, range 1–100):

- **0** — stub/poweroff (reserved; draws must use ≥1)
- **10** — any standard built-in app (clock, etc.)
- **90** — active BUSY/CUSTOM work session

And the empirically-established firmware fact (probed during v1.5, and the
reason the OpenAPI's own "equal priority overrides" text is wrong): **a draw
from a different `application_name` is accepted only at a *strictly greater*
priority than the app currently holding the screen; equal priority is rejected.**

**SPK-3 resolved on-device (see §5b):** the panel's black/resting state — both
true idle and the CI silence gap — rests at **priority 0** (a `@1` draw won all
16/16 times the panel was black). So the filler does **not** need to sit above
the built-in tier to fill gaps. Chosen:

```python
PRIORITY_FILLER = 5    # stub (0) < FILLER (5) < built-in apps (10) < AMBIENT (20)
```

This satisfies the operator's explicit preference — **never override a built-in
app.** A filler at 5 is rejected by anything at ≥ 5 from another app, so a
built-in app (10) keeps the screen; the filler only wins the genuinely-empty
(priority-0) panel.

Resulting arbitration (all automatic via strictly-greater-priority):

| App drawing | Priority | vs. filler (5) |
|---|---|---|
| stub / empty (idle **and** CI gap) | 0 | filler wins → **Nyan fills** |
| **nyan_filler** | **5** | — |
| built-in idle app (clock/desktop) | 10 | preempts Nyan → clock kept, **not overridden** |
| calendar event / CI overlay | 20 / 21 | preempts Nyan |
| calendar approach / raised | 25 | preempts Nyan |
| CI alert (fail/stuck) | 60 | preempts Nyan |
| calendar imminent (urgent) | 65 | preempts Nyan |
| BUSY/CUSTOM work session | 90 | preempts Nyan |

## 7. Animation asset (native `.anim`, resolved)

The animation lives on the device as a native `.anim` file and self-loops. Draw:

```json
{"id": "nyan", "type": "animation", "path": "nyan_72x16.anim", "loop": true,
 "x": 0, "y": 0, "timeout": <T>}
```

**Authoring pipeline (build-time, no runtime dependency).** The device `.anim`
is the firmware's `bicycle0` container. busylib-py's converter does **not**
support animation (`video.py` raises `NotImplementedError`), so we generate the
`.anim` with a small in-repo encoder **ported from the firmware web draw-tool's
`seq2anim.ts`** (`busy-app/busybar-firmware` `assets/frontend/util/seq2anim.ts`),
validated end-to-end on-device (§5b, SPK-4):

- **Frame generation** (`tools/gen_nyan_frames.py`, dev tool): render a fixed
  ~24-frame Nyan loop as `frame_0.png … frame_n.png` (72×16 RGB), reusing the
  reference renderer's geometry (`maxswinkels/busybar-apps` `apps/nyan-cat`
  `_blank/_tick_stars/_rainbow/_cat`). Uses Pillow (a dev-only dependency).
- **Encoding** (`tools/anim_encoder.py`, dev tool): PNG frames + meta → `.anim`.
  `bicycle0` header (36 bytes: magic, `flags`, `width`, `height`, `color_mode`
  `0=rgb888`, `fps`, `max_encoded_len` u16, pad, `sections_len`/`frames_len`/
  `n_sections`/`n_encoded`/`n_display` u32×5), a `default` section, then frames.
  Pixels are packed **BGR** (RGBA→BGR). Frames use **`encoding=0` (raw)** — the
  format's uncompressed path — so no RLE is needed (~83 KB for 24 frames, far
  under the 2 MB stock sizes); RLE is a possible future size optimization only.
- The resulting `assets/nyan/nyan_72x16.anim` (and its source frames) are
  committed. The shipped integration uses stdlib + `BusyBarClient` only;
  Pillow/the encoder are dev tools, not runtime deps.

**Install step:** upload the `.anim` once per device via
`POST /api/assets/upload?application_name=nyan_filler&file=nyan_72x16.anim`
(documented in the integration README; the loop tolerates a missing asset — §11).

The three spikes that gated this are all resolved on-device — see §5b. In
particular `AnimationElement` rendering, the `.anim` upload, redraw-continues,
and the priority floor were each verified against the live device.

**Fallback (now unlikely, kept for completeness):** if a custom `.anim` proves
impractical to generate, pre-stage a fixed loop of PNG frames and play them with
draw-only `ImageElement` swaps at ~6–8 fps — still far cheaper than the
reference app's per-frame encode+upload. Not needed given SPK-1 succeeded.

## 8. Filler loop & redraw policy

`main.py` loop, one wake per `poll_seconds`:

1. If in a **quiet-hours** window → ensure the panel is released (`client.clear("nyan_filler")`
   once, on entry) and sleep. No HTTP draw.
2. Else → **reclaim if dark.** Attempt/maintain the Nyan draw at `PRIORITY_FILLER`.
   - `DRAWN` → Nyan is up and self-looping on-device.
   - `409` (something higher owns the screen) → expected and silent.
   - device unreachable → back off (5 s → ×2 → cap 300 s), same pattern as `ci_status`.

**Redraw-without-stutter — settled by SPK-2 (redraw continues):** the loop
redraws every tick **unconditionally**. When Nyan is already playing, the
same-element redraw continues the on-device loop seamlessly (no restart); when
preempted it 409s (harmless); when a black gap opens the redraw lands and Nyan
appears within one poll. No framebuffer-read gating is needed. Draw with
`loop: true` and `timeout ≈ 2 × poll_seconds` (so a dead poller's frame
self-clears quickly, per the ambient-tier convention) — the every-tick redraw
refreshes it long before it expires.

The pure decision (the quiet-hours gate) lives in `logic.py` and is unit-tested;
the HTTP calls stay in `main.py`.

## 9. Config — `[nyan_filler]`

```toml
[nyan_filler]
enabled = true
poll_seconds = 1              # how quickly a dark gap is reclaimed (≤ this many seconds)
quiet_hours = "00:00-07:00"   # local time; "" disables quiet hours entirely
# opacity = 100               # optional AnimationElement opacity (0–100)
```

Defaults chosen by the operator: `quiet_hours = "00:00-07:00"`, `poll_seconds = 1`.
`enabled = false` makes the agent a no-op (clears once and idles) without
uninstalling it.

## 10. Quiet-hours semantics

- Parsed as `"HH:MM-HH:MM"` in the host's **local** time.
- **Midnight-wrap supported:** `"23:00-07:00"` means 23:00→07:00 crossing
  midnight; `"00:00-07:00"` is the simple same-day case. A window whose start ==
  end is treated as "never quiet" (not "always quiet").
- Empty string `""` disables quiet hours (always eligible to fill).
- On **entering** a quiet window the loop issues one `clear` so the last Nyan
  frame doesn't persist frozen; thereafter it stays silent until the window ends.
- Boundary rule: start is inclusive, end is exclusive (`start ≤ now < end`), so
  a `07:00` end means the filler is eligible again exactly at 07:00.

## 11. Error handling

- **Device unreachable:** exponential backoff (5 s → 300 s cap), identical to
  `ci_status`; the summary line ends `unreachable` so `main` distinguishes it.
- **409 low-priority:** normal operation (something real is on screen); logged at
  debug, not warning.
- **Asset missing** (draw returns an asset error): log a single warning naming the
  expected asset path and the upload command; keep looping (the panel just stays
  whatever it was). The one-time asset upload is an install step, documented in
  the integration README, not done on every boot.
- Never print `config.toml` contents (consistent with the repo-wide rule).

## 12. Machine-cost analysis (why `poll_seconds = 1` is fine)

The loop is a **mostly-sleeping process**. Per second it does exactly one thing:
one ~200-byte animation draw (which either lands as DRAWN or is rejected 409 —
both tiny), or — during quiet hours — a clock check with **no** HTTP call. No
framebuffer reads (SPK-2 made them unnecessary). Worst case ≈ 1 small HTTP
round-trip/second to a **local USB-Ethernet device**.

For comparison, the reference Nyan app pushes ~25 requests/second (full-frame
PNG encode + upload each). This design is ~1–2 orders of magnitude lighter and
lighter than the two agents already running (`ci_status`, `calendar_countdown`).
`poll_seconds = 1` does not materially affect the machine; it is accepted as the
default. (It remains a knob: a larger value reduces calls further at the cost of
slower gap reclaim — the operator preferred fast reclaim.)

## 13. Testing

Pure-logic unit tests (`tests/test_nyan_logic.py`) — the part that carries risk:
- Quiet-hours gate: same-day window, midnight-wrap window, empty window,
  start==end, and the inclusive-start/exclusive-end boundaries.
- Config parsing/validation for the `[nyan_filler]` section (bad `quiet_hours`
  string → clear error, not a crash).

Device-facing code (`main.py` HTTP calls, the asset upload) stays thin and is
exercised by the on-device verification pass, matching how the other two
integrations are tested. `PRIORITY_FILLER` selection is device-behavioral and
verified in SPK-3, not unit-tested.

## 14. On-device verification plan

1. **SPK-1/2/3** as above (format, redraw semantics, priority).
2. **Alternation:** during a real CI run, confirm the panel shows
   badge → Nyan → quota → Nyan → … with no black gaps, ~10 s per frame.
3. **Quiet hours:** set a window covering "now," confirm the panel goes dark and
   stays dark; step past the end, confirm Nyan resumes.
4. **Preemption:** confirm a calendar approach/imminent draw and a CI alert each
   cleanly take the screen from Nyan, and Nyan resumes after they clear.
5. Frame-capture check that Nyan's ink is present and the asset renders at 0,0
   without clipping (reuse the existing capture harness).

## 15. Open questions / risks

- **All three spikes are resolved (§5b)** — native `.anim` renders, redraw
  continues, priority floor is 0. No open feasibility risk remains.
- **Frame authoring quality** is the remaining craft item: baking a ~24-frame
  Nyan loop that reads well and loops without an obvious seam (the reference
  renderer uses random star positions, so a baked loop won't wrap perfectly —
  acceptable for a decorative filler; tune frame count during implementation).
- **Quiet-hours uses host local time**, which follows the Mac's timezone; no DST
  handling beyond what the OS clock provides (acceptable — it's decorative).

## 16. Out of scope / future

- Multiple/selectable animations, or sourcing other `busybar-apps` effects.
- Reacting to device state (brightness, session) beyond priority arbitration.
- Running the loop on a homelab host instead of the Mac (possible only once the
  bar is LAN-reachable rather than USB-tethered; noted, not built).
