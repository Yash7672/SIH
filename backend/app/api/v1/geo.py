"""GET /api/v1/geo/heat - density grid for the police Maps page.

Two layers:

* ``traffic`` - average vehicles visible per processed frame, per ~110 m cell.
  Comes from `traffic_cells`, which the live scanner fills with counts only.
* ``stolen``  - time-decayed sightings of hot-listed plates, from the existing
  `sightings` table. Weight is ``exp(-age_hours / tau)``.

Both return the same shape so the client can render them with one code path.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.deps import require_cop
from app.core.config import settings
from app.core.logging import get_logger
from app.db.session import get_db
from app.models import User
from app.services.cache import cache_service
from app.services.traffic_service import (
    HEAT_CLASSES,
    MIN_FRAMES_PER_CELL,
    heat_tau_hours,
)

logger = get_logger(__name__)
router = APIRouter()

MAX_RANGE_DAYS = 7
MAX_CELLS = 5000
CACHE_TTL_SECONDS = 10
VALID_LAYERS = ("traffic", "stolen")


def _parse_bbox(raw: Optional[str]) -> Optional[Tuple[float, float, float, float]]:
    """`south,west,north,east` - the order every geo API in the world uses."""
    if not raw:
        return None
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) != 4:
        raise HTTPException(422, "bbox must be south,west,north,east")
    try:
        south, west, north, east = (float(p) for p in parts)
    except ValueError:
        raise HTTPException(422, "bbox coordinates must be numbers")
    if not (-90 <= south <= 90 and -90 <= north <= 90):
        raise HTTPException(422, "bbox latitude out of range")
    if not (-180 <= west <= 180 and -180 <= east <= 180):
        raise HTTPException(422, "bbox longitude out of range")
    if south >= north or west >= east:
        raise HTTPException(422, "bbox must be south<north and west<east")
    return south, west, north, east


def _parse_window(
    from_: Optional[datetime], to: Optional[datetime]
) -> Tuple[datetime, datetime]:
    """Default to the last 15 minutes so the endpoint is useful with no params."""
    now = datetime.now(timezone.utc)
    end = to or now
    start = from_ or (end - timedelta(minutes=15))
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    if start >= end:
        raise HTTPException(422, "from must be earlier than to")
    if (end - start) > timedelta(days=MAX_RANGE_DAYS):
        raise HTTPException(422, f"range must not exceed {MAX_RANGE_DAYS} days")
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def _cache_key(layer, start, end, vehicle_class, bbox) -> str:
    parts = [
        "rakshak:heat",
        layer,
        start.isoformat(),
        end.isoformat(),
        vehicle_class or "all",
        ",".join(f"{v:g}" for v in bbox) if bbox else "world",
    ]
    return ":".join(parts)


@router.get("/heat")
def heat(
    layer: str = Query("traffic", pattern="^(traffic|stolen)$"),
    from_: Optional[datetime] = Query(None, alias="from"),
    to: Optional[datetime] = Query(None),
    vehicle_class: Optional[str] = Query(None),
    bbox: Optional[str] = Query(None),
    _: User = Depends(require_cop),
    db: Session = Depends(get_db),
):
    """Heat cells for the requested layer and window.

    COP and ADMIN only: a density map is an operational picture of where cars
    are, which is not something a citizen or a volunteer account should be able
    to read. No token gives 401 from `require_cop` before this runs.
    """
    start, end = _parse_window(from_, to)
    box = _parse_bbox(bbox)

    if vehicle_class is not None and vehicle_class not in HEAT_CLASSES:
        raise HTTPException(422, f"vehicle_class must be one of {', '.join(HEAT_CLASSES)}")

    key = _cache_key(layer, start, end, vehicle_class, box)
    cached = _cache_get(key)
    if cached is not None:
        return cached

    if layer == "traffic":
        payload = _traffic_heat(db, start, end, vehicle_class, box)
    else:
        payload = _stolen_heat(db, start, end, box)

    payload["generated_at"] = datetime.now(timezone.utc).isoformat()
    _cache_set(key, payload)
    return payload


def _cache_get(key: str):
    """Redis is optional; a cache outage must not fail the request."""
    return cache_service.get_json(key)


def _cache_set(key: str, payload: dict) -> None:
    try:
        cache_service.set_json(key, payload, CACHE_TTL_SECONDS)
    except Exception:  # noqa: BLE001
        logger.debug("Heat cache write failed", exc_info=True)


def _cap(cells: list) -> list:
    """Hard cap on the response size.

    Past this point the grid is denser than the map can show anyway, and an
    unbounded payload is how a heat endpoint takes the dashboard down.
    """
    if len(cells) <= MAX_CELLS:
        return cells
    cells.sort(key=lambda c: c["w"], reverse=True)
    return cells[:MAX_CELLS]


def _utc_hour(value: datetime) -> datetime:
    """Floor a UTC-aware timestamp to its UTC hour.

    Deliberately done in Python rather than with `date_trunc('hour', ts)`:
    date_trunc on a timestamptz truncates in the *session* timezone, which here
    is Asia/Calcutta, so a UTC-bucketed table and an IST-truncated window
    disagree by half an hour and silently return nothing.
    """
    return value.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def _traffic_heat(db: Session, start, end, vehicle_class, box) -> dict:
    # w is vehicles per frame, not a raw total: a cell watched for ten minutes
    # and a cell watched for one minute have to be comparable, and only the
    # per-frame average is.
    count_expr = vehicle_class or "two_wheeler + car + bus + truck"
    # Rows are hour buckets, so the window is snapped to the hours it touches.
    # Filtering buckets by the raw timestamp would drop the hour containing
    # `start`, and a 15-minute query against an hourly grid would almost always
    # come back empty.
    sql = text(
        f"""
        SELECT cell_lat, cell_lng,
               (SUM({count_expr})::float / NULLIF(SUM(frames), 0)) AS density,
               SUM(frames)::float AS frames,
               SUM(frames)::int   AS n
        FROM traffic_cells
        WHERE hour_bucket >= :start_hour
          AND hour_bucket <= :end_hour
          AND frames >= :min_frames
          {"AND cell_lat >= :south AND cell_lat <= :north AND cell_lng >= :west AND cell_lng <= :east" if box else ""}
        GROUP BY cell_lat, cell_lng
        ORDER BY density DESC
        LIMIT :limit
        """
    )
    params: dict = {
        "start_hour": _utc_hour(start),
        "end_hour": _utc_hour(end),
        "min_frames": MIN_FRAMES_PER_CELL,
        "limit": MAX_CELLS + 1,
    }
    if box:
        params.update(south=box[0], west=box[1], north=box[2], east=box[3])

    rows = db.execute(sql, params).fetchall()
    cells = []
    for r in rows:
        if r.density is None:
            continue
        cells.append(
            {"lat": float(r.cell_lat), "lng": float(r.cell_lng), "w": float(r.density), "n": int(r.n)}
        )
    cells = _cap(cells)
    return {
        "cells": cells,
        "max": max((c["w"] for c in cells), default=0.0),
        "min": min((c["w"] for c in cells), default=0.0),
        # Declared so the client can label the control honestly.
        "bucket": "hour",
    }


def _stolen_heat(db: Session, start, end, box) -> dict:
    """Hot-listed sightings, decayed so last hour outweighs yesterday.

    Sightings are individually located (they belong to a reported car) but
    bucketed to the same ~110 m grid as traffic so the two layers overlay.
    """
    tau = heat_tau_hours()
    # exp decay in SQL; age_hours is computed against now() so "now" is the
    # database's clock, not this process's.
    sql = text(
        f"""
        WITH recent AS (
            SELECT s.latitude, s.longitude, s.detected_at
            FROM sightings s
            WHERE s.detected_at >= :start AND s.detected_at <= :end
              {"AND s.latitude >= :south AND s.latitude <= :north AND s.longitude >= :west AND s.longitude <= :east" if box else ""}
        )
        SELECT ROUND(latitude::numeric, 3)::float AS lat,
               ROUND(longitude::numeric, 3)::float AS lng,
               SUM(EXP(-EXTRACT(EPOCH FROM (now() - detected_at)) / 3600.0 / :tau))::float AS weight,
               COUNT(*)::int AS n
        FROM recent
        GROUP BY 1, 2
        ORDER BY weight DESC
        LIMIT :limit
        """
    )
    params: dict = {"start": start, "end": end, "tau": tau, "limit": MAX_CELLS + 1}
    if box:
        params.update(south=box[0], west=box[1], north=box[2], east=box[3])

    rows = db.execute(sql, params).fetchall()
    cells = [
        {"lat": float(r.lat), "lng": float(r.lng), "w": float(r.weight), "n": int(r.n)}
        for r in rows
        if r.weight is not None and not math.isnan(float(r.weight))
    ]
    cells = _cap(cells)
    return {
        "cells": cells,
        "max": max((c["w"] for c in cells), default=0.0),
        "min": min((c["w"] for c in cells), default=0.0),
        "tau_hours": tau,
    }
