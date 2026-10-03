"""Optional PostGIS support.

PostGIS is *never* required. On startup we try ``CREATE EXTENSION IF NOT EXISTS
postgis`` in a try/except and remember the answer in a module-level flag. When
it is present the geo queries use ``ST_SnapToGrid`` / ``ST_MakeLine``; when it
is not, the identical API output is produced with plain lat/lng columns and
``FLOOR(lat / cell)`` grid snapping. Callers must not branch on availability for
anything user-visible - only for which SQL text is issued.
"""

from __future__ import annotations

import math
from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.core.logging import get_logger

logger = get_logger(__name__)

_POSTGIS: Optional[bool] = None


def postgis_available(engine: Engine) -> bool:
    """Try once to enable PostGIS; remember the result for the process."""
    global _POSTGIS
    if _POSTGIS is not None:
        return _POSTGIS
    try:
        with engine.connect() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
            conn.commit()
            version = conn.execute(text("SELECT postgis_version()")).scalar()
        _POSTGIS = True
        logger.info("PostGIS available: %s (ST_SnapToGrid/ST_MakeLine enabled)", version)
    except Exception as exc:
        _POSTGIS = False
        logger.info(
            "PostGIS not available (%s) - using plain lat/lng with FLOOR grid snapping. "
            "The API output is identical.",
            str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__,
        )
    return _POSTGIS


def reset_for_tests() -> None:
    """Forget the cached probe result (test isolation)."""
    global _POSTGIS
    _POSTGIS = None


# Grid snapping -------------------------------------------------------------
# Cell size in degrees per resolution level. 0.01 deg is ~1.1 km of latitude,
# which is about the size of a Hyderabad arterial block; "low" is coarser.
GRID_STEP_DEG = {"low": 0.01, "med": 0.002, "high": 0.0005}


def snap_cell(lat: float, lng: float, res: str = "med") -> tuple[float, float]:
    """Snap a coordinate onto the request's grid.

    Matches PostGIS ``ST_SnapToGrid(geom, step)`` semantics for positive
    coordinates (floor to the grid origin) so both backends agree.
    """
    step = GRID_STEP_DEG.get(res, GRID_STEP_DEG["med"])
    return _snap(lat, step), _snap(lng, step)


def _snap(value: float, step: float) -> float:
    return math.floor(value / step) * step


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in km. Used whenever PostGIS is absent."""
    radius = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(min(1.0, a)))