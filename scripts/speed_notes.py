"""Item 8, stated honestly: what the box timings actually mean.

Three probes were run against the same process and the same one-car scene, and
they disagreed, so this file is the reconciliation rather than a fourth
measurement. Read this before quoting any p95.

What was measured, and what each number is:

1. `live_scan_test.py`, 20 s of moving traffic, 4.2 fps
       box p50/p95 80 / 341 ms          round trip 83 / 377 ms
   A car that moves settles or leaves on its own, so OCR runs rarely. This is
   the steady state and it is the number that matters for the overlay feeling
   attached to the road.

2. `probe_leg_contention.py`, three variants on a FRESH connection each
       plate readable (full frame)      box p50/p95 317 / 1289 ms
       plate cropped out of frame       box p50/p95 118 /  230 ms
       plate region blurred              box p50/p95 122 /  709 ms
   Same 1280 px frame, same one car, three different plate outcomes. The frame
   whose plate reads is 2.7x slower on the box reply than the identical frame
   with the plate cropped away. So the cost is not the vehicle pass - it is the
   plate pass running at the same time on `torch.set_num_threads(2)`.

3. `probe_static_scene.py` was run in the wrong order and says the opposite
   (stopped car 70/169 ms, moving car 84/787 ms). That result is void: both
   phases shared one connection, the moving phase read the plate first and set
   `track.plate_settled = True`, so the "static" phase inherited an already
   settled track, never queued a crop, never ran OCR, and so was never
   contended. It is kept here because a reader comparing the two scripts will
   otherwise think they contradict each other.

The settled picture:

  * box p50 70-84 ms, round trip p50 83-118 ms      -> inside the 150 ms target
  * box p95 169-341 ms steady state                  -> over the target
  * the first ~2 s after a newly-seen car's plate is
    being read, box p50 jumps to ~317 ms,
    p95 to ~1.3 s                                   -> clearly over
  * after that read succeeds (conf >= 0.8 sets
    `plate_settled`) no further OCR is queued for
    that car and the box timing returns to baseline

So the target is met on the median and missed at p95, and the p95 is OCR
contention, not detection cost. It is not fixed here on purpose: the obvious
lever is `torch.set_num_threads(2)` at backend/app/api/v1/live_scan.py:57, and
raising it would make this 8-core laptop look better while making a 4-core
target machine worse. That is a tuning decision for someone who knows the
hardware, not a bug fix.

Run: python scripts/speed_notes.py
"""

from __future__ import annotations

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TABLE = """
  scenario                                     box p50    box p95    verdict vs 150 ms
  -------------------------------------------------------------------------------
  moving traffic, 4.2 fps (live_scan_test)          80 ms      341 ms   p50 pass, p95 miss
  1 car, plate cropped out of frame                118 ms      230 ms   p50 pass, p95 miss
  1 car, plate readable                            317 ms     1289 ms   both miss
  1 car, plate region blurred                      122 ms      709 ms   p50 pass, p95 miss

  the last two are the same 1280 px frame with the plate cropped away or
  smeared. 2.7x on the box reply, from the plate pass sharing two torch
  threads with the vehicle pass.
"""


def main() -> int:
    print("\n=== item 8: how the speed numbers were arrived at ===")
    print(TABLE)
    print("  what would move it, and why it is not done here:")
    print(
        "    backend/app/api/v1/live_scan.py:57  torch.set_num_threads(2)"
    )
    print(
        "    raising it to 3-4 clears most of the p95 on this 8-core laptop and"
    )
    print(
        "    pushes the same work onto a 4-core target. Needs the real hardware."
    )
    print(
        "    the alternative - moving OCR to its own process - removes the"
    )
    print(
        "    contention outright but doubles the resident model set. Also not a"
    )
    print("    small fix.\n")
    print("  scripts that produce these numbers:")
    for name in ("live_scan_test.py", "probe_leg_contention.py", "probe_static_scene.py", "speed_check.py"):
        path = os.path.join(ROOT, "scripts", name)
        print(f"    {name:<28} {'present' if os.path.exists(path) else 'MISSING'}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())