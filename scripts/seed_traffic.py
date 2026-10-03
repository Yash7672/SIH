"""Seed synthetic traffic density so the Maps heatmap has something to show.

The live scanner only records cells for phones that actually point a camera at a
road, which on a fresh demo database is nothing. This fills a plausible
Hyderabad-area grid for the last ``--hours`` so ``/maps`` renders immediately.

Every row it writes is tagged ``synthetic = true`` and can be removed exactly:

    python scripts/seed_traffic.py --purge

Idempotent: re-running accumulates into the same (cell, hour, synthetic) row
rather than duplicating it, and ``--purge`` clears the marker before writing.
"""
from __future__ import annotations

import argparse
import logging
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "backend"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

from sqlalchemy import text  # noqa: E402

from app.core.logging import get_logger, setup_logging  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.services.traffic_service import (  # noqa: E402
    RETENTION_DAYS,
    hour_bucket,
    purge_older_than,
    round_cell,
    usable_position,
)

logger = get_logger("seed_traffic")

# A handful of real Hyderabad corridors. Each is (name, lat, lng, spread_deg,
# peak vehicles/frame). Enough structure for the heatmap to look like a city
# rather than random noise.
CORRIDORS = [
    ("MGBS-Hitec City", 17.4475, 78.3765, 0.055, 9.0),
    ("Jubilee Hills-Kondapur", 17.4400, 78.4100, 0.045, 7.5),
    ("Gachibowli-Botanical Garden", 17.4650, 78.3600, 0.050, 8.0),
    ("Secunderabad railway station", 17.4390, 78.4980, 0.030, 11.0),
    ("Uppal-Allahabad Outer Ring Rd", 17.4000, 78.5500, 0.060, 6.5),
    ("Kukatpally-Balanagar", 17.4650, 78.4100, 0.040, 6.0),
    ("Ameerpet-Sainikpuri", 17.4400, 78.4900, 0.035, 5.5),
    ("Shamshabad airport road", 17.3400, 78.4300, 0.050, 4.0),
]


def synth_frame(hour: int, peak: float, rng: random.Random) -> dict:
    """Plausible per-frame counts for one hour.

    Traffic has a diurnal shape: near-empty at 04:00, a morning ramp, a flat
    afternoon and an evening peak. A constant count per cell would make the
    time-window control on the Maps page meaningless.
    """
    profile = [
        0.10, 0.06, 0.05, 0.06, 0.12, 0.30, 0.62, 0.95, 1.00,
        0.78, 0.70, 0.75, 0.80, 0.72, 0.68, 0.80, 0.98, 1.00,
        0.92, 0.80, 0.66, 0.48, 0.34, 0.18,
    ]
    base = peak * profile[hour % 24]
    # Frames per cell-hour: enough to clear MIN_FRAMES_PER_CELL everywhere.
    frames = rng.randint(90, 320)

    def split(total: int, weights: tuple) -> list:
        out = []
        remaining = total
        for i, w in enumerate(weights[:-1]):
            v = int(total * w)
            out.append(v)
            remaining -= v
        out.append(max(0, remaining))
        return out

    two_w, car, bus, truck = split(
        int(round(frames * base * rng.uniform(0.75, 1.25))),
        (0.42, 0.44, 0.08, 0.06),
    )
    return {
        "frames": frames,
        "two_wheeler": two_w,
        "car": car,
        "bus": bus,
        "truck": truck,
    }


def seed(hours: int, seed_value: int, purge: bool) -> int:
    rng = random.Random(seed_value)
    db = SessionLocal()
    written = 0
    try:
        if purge:
            removed = db.execute(
                text("DELETE FROM traffic_cells WHERE synthetic IS TRUE")
            ).rowcount
            db.commit()
            logger.info("Purged %d synthetic rows", removed)

        now = datetime.now(timezone.utc)
        values = []
        for _name, lat, lng, spread, peak in CORRIDORS:
            # A few grid cells per corridor, scattered along its length.
            cells = max(3, int(round(0.12 / spread)))
            for c in range(cells):
                # Deterministic-ish scatter inside the corridor's bounding box.
                clat = lat + rng.uniform(-spread, spread)
                clng = lng + rng.uniform(-spread, spread)
                if not usable_position(clat, clng):
                    continue
                cell_lat, cell_lng = round_cell(clat, clng)
                for back in range(hours):
                    when = now - timedelta(hours=back)
                    bucket = hour_bucket(when)
                    row = synth_frame(when.hour, peak, rng)
                    values.append(
                        {
                            "cell_lat": cell_lat,
                            "cell_lng": cell_lng,
                            "hour_bucket": bucket,
                            **row,
                            "synthetic": True,
                        }
                    )

        # Same ON CONFLICT the live accumulator uses, so seeding twice in the
        # same hour accumulates instead of violating the unique constraint.
        stmt = text(
            """
            INSERT INTO traffic_cells
                (cell_lat, cell_lng, hour_bucket, frames, two_wheeler, car, bus, truck, synthetic)
            VALUES (:cell_lat, :cell_lng, :hour_bucket, :frames, :two_wheeler, :car, :bus, :truck, :synthetic)
            ON CONFLICT (cell_lat, cell_lng, hour_bucket, synthetic) DO UPDATE SET
                frames      = traffic_cells.frames      + EXCLUDED.frames,
                two_wheeler = traffic_cells.two_wheeler + EXCLUDED.two_wheeler,
                car         = traffic_cells.car         + EXCLUDED.car,
                bus         = traffic_cells.bus         + EXCLUDED.bus,
                truck       = traffic_cells.truck       + EXCLUDED.truck
            """
        )
        for chunk_start in range(0, len(values), 500):
            chunk = values[chunk_start : chunk_start + 500]
            db.execute(stmt, chunk)
            written += len(chunk)
        db.commit()

        total = db.execute(text("SELECT count(*) FROM traffic_cells")).scalar()
        real = db.execute(
            text("SELECT count(*) FROM traffic_cells WHERE synthetic IS FALSE")
        ).scalar()
        logger.info(
            "Seeded %d synthetic cells (%d hours x %d corridors); table now has %d rows "
            "(%d real, %d synthetic)",
            written, hours, len(CORRIDORS), total, real, total - real,
        )
    finally:
        db.close()
    return written


def purge_all() -> int:
    db = SessionLocal()
    try:
        removed = db.execute(
            text("DELETE FROM traffic_cells WHERE synthetic IS TRUE")
        ).rowcount
        db.commit()
        logger.info("Removed %d synthetic rows", removed)
        return removed
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=int, default=24, help="hours of history to synthesise (default 24)")
    parser.add_argument("--seed", type=int, default=1337, help="RNG seed for reproducible maps")
    parser.add_argument("--purge", action="store_true", help="delete synthetic rows before seeding")
    parser.add_argument("--purge-only", action="store_true", help="delete synthetic rows and exit")
    parser.add_argument("--prune", action="store_true", help=f"also drop rows older than {RETENTION_DAYS} days")
    args = parser.parse_args()

    setup_logging(logging.INFO)
    if args.hours < 1:
        logger.error("--hours must be at least 1")
        return 2
    if args.hours > 24 * 31:
        logger.error("--hours above 744 would fall outside the retention window")
        return 2

    if args.prune:
        db = SessionLocal()
        try:
            removed = purge_older_than(db)
            logger.info("Pruned %d rows older than %d days", removed, RETENTION_DAYS)
        finally:
            db.close()

    if args.purge_only:
        purge_all()
        return 0

    written = seed(args.hours, args.seed, args.purge)
    print(f"seeded {written} synthetic traffic cells ({args.hours}h)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())