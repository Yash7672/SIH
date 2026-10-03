"""Traffic-density accumulation for the Maps heatmap.

The live scanner pushes roughly 15 frames a second per phone. Writing one row per
frame would be thousands of statements a minute for data that is only ever read
as an hourly aggregate, so counts are accumulated in memory per
(cell, hour, class) and flushed on a timer, on disconnect and at shutdown.

Privacy: this module only ever receives a coordinate and a per-class tally. No
plate, no image and no device id reaches `traffic_cells`, and a density grid
cannot be joined back to an individual vehicle.
"""
from __future__ import annotations

import math
import threading
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, Mapping, Tuple

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# 0.001 degrees is about 110 m at the equator - a city block. Coarser than the
# GPS error of a phone, which is the point: the grid must not be finer than the
# measurement feeding it.
CELL_PRECISION = 3
FLUSH_INTERVAL_SECONDS = 5.0
RETENTION_DAYS = 90
# A cell needs this many frames before it is shown, otherwise one frame with two
# cars in it paints as bright a hot spot as a junction with 200 frames.
MIN_FRAMES_PER_CELL = 3

# COCO class ids that count as "a vehicle" for the density map.
VEHICLE_CLASSES: Dict[str, int] = {
    "motorcycle": 1,
    "bicycle": 2,
    "car": 3,
    "bus": 6,
    "truck": 8,
}
# Classes the heat endpoint can filter on.
HEAT_CLASSES = ("two_wheeler", "car", "bus", "truck")

_COUNTS = ("two_wheeler", "car", "bus", "truck")

# India's bounding box, padded generously. Anything outside it is a bad GPS fix
# (0,0, a stale cached fix, or a laptop's location service), not a road.
INDIA_LAT = (6.0, 37.5)
INDIA_LNG = (68.0, 98.0)


def round_cell(lat: float, lng: float) -> Tuple[float, float]:
    """Snap a coordinate to the grid, rounding half away from zero."""
    return (
        round(lat, CELL_PRECISION),
        round(lng, CELL_PRECISION),
    )


def usable_position(lat: object, lng: object) -> bool:
    """True only for a fix we are willing to attribute traffic to.

    Phones genuinely produce 0/0, null and stale cached positions. Crediting
    those to a cell would paint a hot spot in the middle of the ocean, so the
    frame is counted as a frame (density still drops) but not as a location.
    """
    if not isinstance(lat, (int, float)) or not isinstance(lng, (int, float)):
        return False
    if isinstance(lat, bool) or isinstance(lng, bool):
        return False
    if not (math.isfinite(lat) and math.isfinite(lng)):
        return False
    # 0,0 is the classic "no fix" sentinel from the location API.
    if abs(lat) < 1e-6 and abs(lng) < 1e-6:
        return False
    if not (INDIA_LAT[0] <= lat <= INDIA_LAT[1]):
        return False
    if not (INDIA_LNG[0] <= lng <= INDIA_LNG[1]):
        return False
    return True


def hour_bucket(when: datetime | None = None) -> datetime:
    """The UTC hour a timestamp belongs to."""
    when = when or datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    when = when.astimezone(timezone.utc)
    return when.replace(minute=0, second=0, microsecond=0)


def tally_from_boxes(boxes: Iterable[Mapping]) -> Dict[str, int]:
    """Count detections per heat class from a frame's vehicle boxes."""
    out = {k: 0 for k in _COUNTS}
    for item in boxes or ():
        if not isinstance(item, Mapping):
            continue
        cid = item.get("cls_id", item.get("class_id", item.get("cls")))
        try:
            cid = int(cid)
        except (TypeError, ValueError):
            continue
        if cid == VEHICLE_CLASSES["motorcycle"] or cid == VEHICLE_CLASSES["bicycle"]:
            out["two_wheeler"] += 1
        elif cid == VEHICLE_CLASSES["car"]:
            out["car"] += 1
        elif cid == VEHICLE_CLASSES["bus"]:
            out["bus"] += 1
        elif cid == VEHICLE_CLASSES["truck"]:
            out["truck"] += 1
    return out


class TrafficAccumulator:
    """Thread-safe in-memory tally with a batched, idempotent flush.

    `add` is called from the WebSocket's inference threads and never touches the
    database; `flush` is the only writer.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # (lat, lng, bucket) -> {"frames": int, "two_wheeler": int, ...}
        self._pending: Dict[Tuple[float, float, datetime], Dict[str, int]] = {}
        self._dropped_positions = 0
        self._frames_seen = 0

    # ---------------------------------------------------------------- record

    def add(
        self,
        lat: object,
        lng: object,
        vehicles: Iterable[Mapping] | Mapping | None = None,
        when: datetime | None = None,
    ) -> bool:
        """Record one processed frame. Returns True when it carried a usable position."""
        if isinstance(vehicles, Mapping):
            counts = {k: int(vehicles.get(k, 0) or 0) for k in _COUNTS}
        else:
            counts = tally_from_boxes(vehicles or ())

        key_ok = usable_position(lat, lng)
        with self._lock:
            self._frames_seen += 1
            if not key_ok:
                self._dropped_positions += 1
                return False
            cell = (round_cell(float(lat), float(lng)), hour_bucket(when))
            row = self._pending.setdefault(
                cell,
                {"frames": 0, "two_wheeler": 0, "car": 0, "bus": 0, "truck": 0},
            )
            # frames counts every processed frame, including frames with no
            # vehicles, so an empty road reads as low density rather than as a gap.
            row["frames"] += 1
            for k in _COUNTS:
                row[k] += counts.get(k, 0)
        return True

    # ----------------------------------------------------------------- flush

    @property
    def pending_cells(self) -> int:
        with self._lock:
            return len(self._pending)

    def stats(self) -> dict:
        with self._lock:
            return {
                "frames_seen": self._frames_seen,
                "dropped_positions": self._dropped_positions,
                "pending_cells": len(self._pending),
            }

    def take(self) -> Dict[Tuple[float, float, datetime], Dict[str, int]]:
        """Detach the pending batch so a failed write does not lose it."""
        with self._lock:
            batch, self._pending = self._pending, {}
        return batch

    def restore(self, batch: Mapping[Tuple[float, float, datetime], Mapping[str, int]]) -> None:
        """Put a batch back after a failed flush, merging rather than replacing."""
        with self._lock:
            for key, row in batch.items():
                target = self._pending.setdefault(
                    key,
                    {"frames": 0, "two_wheeler": 0, "car": 0, "bus": 0, "truck": 0},
                )
                for k in ("frames", *_COUNTS):
                    target[k] += int(row.get(k, 0) or 0)

    def flush(self, db: Session) -> int:
        """Upsert the whole pending batch in one statement. Returns cells written."""
        batch = self.take()
        if not batch:
            return 0

        values = []
        for (cell, bucket), row in batch.items():
            values.append(
                {
                    "cell_lat": cell[0],
                    "cell_lng": cell[1],
                    "hour_bucket": bucket,
                    "frames": int(row.get("frames", 0)),
                    "two_wheeler": int(row.get("two_wheeler", 0)),
                    "car": int(row.get("car", 0)),
                    "bus": int(row.get("bus", 0)),
                    "truck": int(row.get("truck", 0)),
                    "synthetic": False,
                }
            )

        # One INSERT for the whole batch: an ON CONFLICT that adds to whatever is
        # already there, so flushing twice for the same hour accumulates instead
        # of overwriting. Idempotent in the sense that matters here - replaying a
        # batch would double-count, so `take` is what guarantees at-most-once.
        stmt = text(
            """
            INSERT INTO traffic_cells
                (cell_lat, cell_lng, hour_bucket, frames, two_wheeler, car, bus, truck, synthetic)
            VALUES (:cell_lat, :cell_lng, :hour_bucket, :frames, :two_wheeler, :car, :bus, :truck, :synthetic)
            ON CONFLICT (cell_lat, cell_lng, hour_bucket, synthetic) DO UPDATE SET
                frames     = traffic_cells.frames     + EXCLUDED.frames,
                two_wheeler= traffic_cells.two_wheeler+ EXCLUDED.two_wheeler,
                car        = traffic_cells.car        + EXCLUDED.car,
                bus        = traffic_cells.bus        + EXCLUDED.bus,
                truck      = traffic_cells.truck      + EXCLUDED.truck
            """
        )
        try:
            db.execute(stmt, values)
            db.commit()
        except Exception:
            db.rollback()
            self.restore(batch)
            logger.warning("Traffic flush failed, %d cells kept in memory", len(batch))
            raise
        return len(values)


# Process-wide instance shared by every /ws/scan connection.
traffic_accumulator = TrafficAccumulator()


def purge_older_than(db: Session, days: int = RETENTION_DAYS) -> int:
    """Delete density rows past the retention window. Called at startup."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    result = db.execute(
        text("DELETE FROM traffic_cells WHERE hour_bucket < :cutoff"), {"cutoff": cutoff}
    )
    db.commit()
    return result.rowcount or 0


def start_flush_worker(db_factory, stop_event: threading.Event) -> threading.Thread:
    """Flush the accumulator every FLUSH_INTERVAL_SECONDS until stopped.

    Runs off the event loop: the flush is a small blocking write and there is no
    reason to make every request wait behind it.
    """

    def loop() -> None:
        while not stop_event.wait(FLUSH_INTERVAL_SECONDS):
            db = None
            try:
                if traffic_accumulator.pending_cells == 0:
                    continue
                db = db_factory()
                written = traffic_accumulator.flush(db)
                if written:
                    logger.info("Traffic flush wrote %d cells", written)
            except Exception as exc:  # noqa: BLE001 - a failed flush must not kill the thread
                logger.warning("Traffic flush worker error: %s", exc)
            finally:
                if db is not None:
                    db.close()

    thread = threading.Thread(target=loop, name="TrafficFlush", daemon=True)
    thread.start()
    return thread


def heat_tau_hours() -> float:
    """Time-decay constant for the stolen layer. Configurable, sane default 6 h."""
    try:
        value = float(getattr(settings, "HEAT_TAU_HOURS", 6.0))
    except (TypeError, ValueError):
        return 6.0
    return value if value > 0 else 6.0
