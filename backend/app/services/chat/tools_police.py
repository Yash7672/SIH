"""Police toolset (COP, ADMIN): read-only queries plus their quick questions.

Every number here comes from the database. Groq only ever *chooses* one of these
functions and supplies arguments; the arguments are validated and the query runs
in the backend under the caller's own role. Nothing in this module writes.

Where an existing endpoint already computes something, its exact query is reused
rather than rewritten - see docs/PROJECT_MAP.md "API Route Table".
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Complaint,
    ComplaintStatus,
    Device,
    Hotlist,
    HotlistStatus,
    Sighting,
    TrafficCell,
)
from app.services.chat import common
from app.services.chat.common import IST, clamp_limit, humanize_age, now_utc, round_coord
from app.services.plate import is_valid_plate, normalize_plate, plate_key
from app.services.traffic_service import MIN_FRAMES_PER_CELL

ACTIVE_HOTLIST_STATES = (HotlistStatus.ACTIVE, HotlistStatus.FIR_CONFIRMED)
PENDING_STATES = (ComplaintStatus.PENDING, ComplaintStatus.UNDER_REVIEW)

# Mirrors alerts.py: an alert is a hot-list detection from a still-watched vehicle.
ALERT_WINDOW_HOURS = 24


def _answer(
    text: str,
    kpis: Optional[list[dict]] = None,
    links: Optional[list[dict]] = None,
    follow_ups: Optional[list[str]] = None,
    data: Optional[dict] = None,
) -> dict[str, Any]:
    return {
        "text": text,
        "kpis": kpis or [],
        "links": links or [],
        "follow_ups": follow_ups or [],
        "data": data or {},
    }


def not_available(what: str, follow_ups: Optional[list[str]] = None) -> dict[str, Any]:
    return _answer(
        f"{what} is not available yet.",
        follow_ups=follow_ups or ["Today's analytics", "Latest alerts"],
        data={"available": False},
    )


# ---------------------------------------------------------------- quick + tools


def stolen_today(db: Session) -> dict[str, Any]:
    """Active hot-list size, today's sightings on it, and what was added today.

    "Today" is the Asia/Kolkata day (see chat/common.ist_day_bounds); the stored
    timestamps are UTC.
    """
    start, end = common.ist_day_bounds()

    active = db.execute(
        select(func.count()).select_from(Hotlist).where(Hotlist.status.in_(ACTIVE_HOTLIST_STATES))
    ).scalar() or 0

    sight_rows = db.execute(
        select(func.count(), func.count(func.distinct(Sighting.hotlist_id)))
        .where(Sighting.detected_at >= start, Sighting.detected_at < end)
    ).one()
    sightings_today, distinct_today = int(sight_rows[0] or 0), int(sight_rows[1] or 0)

    added_today = db.execute(
        select(func.count()).select_from(Hotlist).where(Hotlist.added_at >= start, Hotlist.added_at < end)
    ).scalar() or 0

    if active == 0:
        text = "There are no vehicles on the active hot-list right now."
    else:
        text = (
            f"{active} vehicle{'s' if active != 1 else ''} on the active hot-list. "
            f"Today: {sightings_today} sighting{'s' if sightings_today != 1 else ''}"
            f" across {distinct_today} vehicle{'s' if distinct_today != 1 else ''}, "
            f"and {added_today} newly hot-listed."
        )
    return _answer(
        text,
        kpis=[
            {"label": "Active hot-list", "value": active},
            {"label": "Sightings today", "value": sightings_today},
            {"label": "Vehicles seen today", "value": distinct_today},
            {"label": "Newly hot-listed", "value": added_today},
        ],
        links=[{"label": "Open Hotlist", "path": "/hotlist"}],
        follow_ups=["Latest alerts", "Top hot-listed vehicles", "Busiest areas today"],
        data={
            "active_hotlist": active,
            "sightings_today": sightings_today,
            "distinct_vehicles_today": distinct_today,
            "newly_hotlisted_today": added_today,
        },
    )


def analytics_today(db: Session) -> dict[str, Any]:
    """Detections, hot-list matches, class mix, busiest hour and live devices.

    Reuses the same aggregate shapes as GET /analytics/overview and
    GET /analytics/traffic so the numbers agree with the dashboard.
    """
    start, end = common.ist_day_bounds()

    detections = db.execute(
        select(func.count()).select_from(Sighting).where(Sighting.detected_at >= start, Sighting.detected_at < end)
    ).scalar() or 0

    # Per-hour detections today, floored in UTC (never date_trunc on a
    # timestamptz - see the timezone note in docs/PROJECT_MAP.md).
    hour_rows = db.execute(
        select(Sighting.detected_at).where(Sighting.detected_at >= start, Sighting.detected_at < end)
    ).scalars()
    buckets: dict[Any, int] = {}
    for ts in hour_rows:
        buckets[ts.astimezone(IST).replace(minute=0, second=0, microsecond=0)] = (
            buckets.get(ts.astimezone(IST).replace(minute=0, second=0, microsecond=0), 0) + 1
        )
    busiest = max(buckets.items(), key=lambda kv: kv[1], default=(None, 0))

    # Anonymous class mix for the last 24 h, exactly as /analytics/traffic builds it.
    volume = TrafficCell.two_wheeler + TrafficCell.car + TrafficCell.bus + TrafficCell.truck
    since = now_utc() - timedelta(hours=24)
    class_mix = {
        name: int(db.execute(select(func.coalesce(func.sum(col), 0)).where(TrafficCell.hour_bucket >= since)).scalar() or 0)
        for name, col in (
            ("two_wheeler", TrafficCell.two_wheeler),
            ("car", TrafficCell.car),
            ("bus", TrafficCell.bus),
            ("truck", TrafficCell.truck),
        )
    }
    has_traffic = sum(class_mix.values()) > 0

    cutoff = now_utc() - timedelta(minutes=5)
    devices = db.execute(
        select(func.count()).select_from(Device).where(Device.revoked.is_(False), Device.last_seen_at >= cutoff)
    ).scalar() or 0

    parts = [f"{detections} hot-list detection{'s' if detections != 1 else ''} today"]
    if busiest[0] is not None:
        parts.append(f"busiest hour {busiest[0].strftime('%H:00')} IST with {busiest[1]}")
    if has_traffic:
        parts.append(f"{sum(class_mix.values())} vehicles counted by class in the last 24 h")
    parts.append(f"{devices} device{'s' if devices != 1 else ''} seen in the last 5 minutes")

    kpis = [
        {"label": "Detections today", "value": detections},
        {"label": "Devices (5 min)", "value": devices},
    ]
    if busiest[0] is not None:
        kpis.append({"label": "Busiest hour", "value": busiest[0].strftime("%H:00") + " IST"})
    if has_traffic:
        kpis.extend(
            [
                {"label": "Two-wheelers", "value": class_mix["two_wheeler"]},
                {"label": "Cars", "value": class_mix["car"]},
            ]
        )

    return _answer(
        "Today: " + "; ".join(parts) + ".",
        kpis=kpis,
        links=[{"label": "Open Analytics", "path": "/analytics"}, {"label": "Open Maps", "path": "/maps"}],
        follow_ups=["Busiest areas today", "Latest alerts", "Devices online"],
        data={
            "detections_today": detections,
            "busiest_hour": _fmt_hour(busiest[0]),
            "busiest_hour_count": busiest[1],
            "devices_last_5_min": devices,
            "class_mix": class_mix if has_traffic else {},
        },
    )


def latest_alerts(db: Session, limit: Optional[int] = 5) -> dict[str, Any]:
    """The most recent hot-list detections - same query as GET /alerts."""
    rows = db.execute(
        select(Sighting, Hotlist)
        .join(Hotlist, Sighting.hotlist_id == Hotlist.id)
        .where(
            Sighting.created_at >= now_utc() - timedelta(hours=ALERT_WINDOW_HOURS),
            Hotlist.status.in_(ACTIVE_HOTLIST_STATES),
        )
        .order_by(Sighting.detected_at.desc())
        .limit(clamp_limit(limit, default=5))
    ).all()

    if not rows:
        return _answer(
            f"No hot-list alerts in the last {ALERT_WINDOW_HOURS} hours.",
            kpis=[{"label": "Alerts (24 h)", "value": 0}],
            links=[{"label": "Open Live Alerts", "path": "/alerts"}],
            follow_ups=["Total stolen vehicles today", "Top hot-listed vehicles"],
            data={"alerts": [], "count": 0},
        )

    lines = []
    for sighting, entry in rows:
        loc = round_coord(sighting.latitude), round_coord(sighting.longitude)
        lines.append(f"{entry.plate} at {loc[0]}, {loc[1]} ({humanize_age(sighting.detected_at)} ago)")

    return _answer(
        f"Last {len(rows)} hot-list alert{'s' if len(rows) != 1 else ''}: " + "; ".join(lines) + ".",
        kpis=[{"label": "Alerts (24 h)", "value": len(rows)}],
        links=[{"label": "Open Live Alerts", "path": "/alerts"}],
        follow_ups=["Total stolen vehicles today", "Busiest areas today", "Top hot-listed vehicles"],
        data={
            "count": len(rows),
            "alerts": [
                {
                    "plate": h.plate,
                    "lat": round_coord(s.latitude),
                    "lng": round_coord(s.longitude),
                    "minutes_ago": common.minutes_since(s.detected_at),
                }
                for s, h in rows
            ],
        },
    )


def top_hotlist(db: Session, limit: Optional[int] = 5) -> dict[str, Any]:
    """Hot-list vehicles ordered by sighting count - same query as /analytics/hotlist-matches."""
    rows = db.execute(
        select(Hotlist.plate, func.count(Sighting.id))
        .join(Sighting, Sighting.hotlist_id == Hotlist.id, isouter=True)
        .where(Hotlist.status.in_(ACTIVE_HOTLIST_STATES))
        .group_by(Hotlist.plate)
        .having(func.count(Sighting.id) > 0)
        .order_by(func.count(Sighting.id).desc())
        .limit(clamp_limit(limit, default=5))
    ).all()

    if not rows:
        return _answer(
            "No hot-list vehicle has been sighted yet.",
            kpis=[{"label": "Vehicles with sightings", "value": 0}],
            links=[{"label": "Open Hotlist", "path": "/hotlist"}],
            follow_ups=["Total stolen vehicles today", "Latest alerts"],
            data={"top": [], "count": 0},
        )

    top = [{"plate": plate, "sightings": int(count or 0)} for plate, count in rows]
    lines = [f"{item['plate']} ({item['sightings']} sightings)" for item in top]
    return _answer(
        f"Most-sighted hot-list vehicles: {', '.join(lines)}.",
        kpis=[{"label": "Top vehicle", "value": top[0]["sightings"]}],
        links=[{"label": "Open Hotlist", "path": "/hotlist"}],
        follow_ups=["Latest alerts", "Total stolen vehicles today"],
        data={"top": top, "count": len(top)},
    )


def busiest_areas(db: Session, limit: Optional[int] = 5) -> dict[str, Any]:
    """Busiest grid cells from traffic_cells - hidden when the layer has no data."""
    since = now_utc() - timedelta(hours=24)
    volume = TrafficCell.two_wheeler + TrafficCell.car + TrafficCell.bus + TrafficCell.truck
    rows = db.execute(
        select(TrafficCell.cell_lat, TrafficCell.cell_lng, func.sum(volume), func.sum(TrafficCell.frames))
        .where(TrafficCell.hour_bucket >= since)
        .group_by(TrafficCell.cell_lat, TrafficCell.cell_lng)
        .having(func.sum(TrafficCell.frames) >= MIN_FRAMES_PER_CELL)
        .order_by(func.sum(volume).desc())
        .limit(clamp_limit(limit, default=5))
    ).all()

    if not rows:
        return not_available("Area density", follow_ups=["Today's analytics", "Latest alerts"])

    cells = [
        {"lat": round_coord(lat), "lng": round_coord(lng), "count": int(count or 0)}
        for lat, lng, count, _frames in rows
    ]
    lines = [f"{c['lat']}, {c['lng']} ({c['count']} vehicles)" for c in cells]
    return _answer(
        "Busiest areas in the last 24 hours: " + "; ".join(lines) + ".",
        kpis=[{"label": "Busiest cell", "value": cells[0]["count"]}],
        links=[{"label": "Open Maps", "path": "/maps"}],
        follow_ups=["Today's analytics", "Latest alerts"],
        data={"cells": cells, "count": len(cells)},
    )


def pending_complaints(db: Session) -> dict[str, Any]:
    """Counts per complaint status plus the age of the oldest open one."""
    rows = db.execute(select(Complaint.status, func.count()).group_by(Complaint.status)).all()
    counts = {status.value: int(count or 0) for status, count in rows}
    for status in ComplaintStatus:
        counts.setdefault(status.value, 0)

    open_total = counts[ComplaintStatus.PENDING.value] + counts[ComplaintStatus.UNDER_REVIEW.value]

    oldest = db.execute(
        select(func.min(Complaint.created_at)).where(Complaint.status.in_(PENDING_STATES))
    ).scalar()

    text = f"{open_total} complaint{'s' if open_total != 1 else ''} waiting (PENDING + UNDER_REVIEW)"
    if oldest is not None:
        text += f"; the oldest has been open {humanize_age(oldest)}"
    text += "."

    return _answer(
        text,
        kpis=[
            {"label": "Pending", "value": counts[ComplaintStatus.PENDING.value]},
            {"label": "Under review", "value": counts[ComplaintStatus.UNDER_REVIEW.value]},
            {"label": "Hot-listed", "value": counts[ComplaintStatus.HOTLISTED.value]},
        ],
        links=[{"label": "Open Complaints", "path": "/complaints"}],
        follow_ups=["Total stolen vehicles today", "Latest alerts"],
        data={
            "by_status": counts,
            "open_total": open_total,
            "oldest_open_minutes": common.minutes_since(oldest) if oldest else None,
        },
    )


def ocr_accuracy(db: Session) -> dict[str, Any]:
    """Latest measured OCR accuracy - only when an evaluation report exists.

    Reuses ``app.api.v1.ocr_metrics``'s report path and the cached
    ``app.services.ocr_metrics.load_report``, which returns a validated
    ``OcrReport`` (or None when no evaluation has been run). Note the argument:
    unlike the endpoint, the report is never generated on a request.
    """
    from app.api.v1.ocr_metrics import REPORT_PATH
    from app.services.ocr_metrics import load_report

    report = _safe_report(load_report, REPORT_PATH)
    if report is None:
        return not_available("OCR accuracy", follow_ups=["Today's analytics", "Devices online"])

    kpis_data = report.kpis
    target = report.target
    plate_acc = kpis_data.plate_accuracy
    headline = target.headline_set or "headline set"

    if plate_acc is None:
        return not_available("OCR accuracy", follow_ups=["Today's analytics"])

    pct = float(plate_acc) * 100
    target_pct = _pct(target.accuracy)
    text = (
        f"Plate read accuracy is {pct:.1f}% on the {headline} set "
        f"(target {target_pct}, {'met' if target.met_point else 'not met'})."
    )
    return _answer(
        text,
        kpis=[
            {"label": "Plate accuracy", "value": f"{pct:.1f}%"},
            {"label": "Character accuracy", "value": _pct(kpis_data.character_accuracy)},
            {"label": "No-read rate", "value": _pct(kpis_data.no_read_rate)},
        ],
        links=[{"label": "Open Analytics", "path": "/analytics"}],
        follow_ups=["Today's analytics", "Devices online"],
        data={
            "plate_accuracy": round(pct, 2),
            "character_accuracy": _pct(kpis_data.character_accuracy),
            "headline_set": headline,
        },
    )


def devices_online(db: Session) -> dict[str, Any]:
    """Non-revoked devices, and how many reported in the last 5 minutes."""
    total = db.execute(select(func.count()).select_from(Device).where(Device.revoked.is_(False))).scalar() or 0
    cutoff = now_utc() - timedelta(minutes=5)
    recent = db.execute(
        select(func.count()).select_from(Device).where(Device.revoked.is_(False), Device.last_seen_at >= cutoff)
    ).scalar() or 0

    if total == 0:
        return _answer(
            "No devices are registered yet.",
            kpis=[{"label": "Registered", "value": 0}, {"label": "Online (5 min)", "value": 0}],
            links=[{"label": "Open Admin", "path": "/admin"}],
            follow_ups=["Today's analytics"],
            data={"registered": 0, "online": 0},
        )

    return _answer(
        f"{total} registered device{'s' if total != 1 else ''}, {recent} seen in the last 5 minutes.",
        kpis=[{"label": "Registered", "value": total}, {"label": "Online (5 min)", "value": recent}],
        links=[{"label": "Open Admin", "path": "/admin"}],
        follow_ups=["Today's analytics", "Latest alerts"],
        data={"registered": total, "online": recent},
    )


def last_seen(db: Session, plate: str) -> dict[str, Any]:
    """Where a *hot-listed* plate was last seen.

    A plate that is not on the hot-list returns an explicit "no record" answer.
    That is the privacy design, not a gap: non-hot-listed plates are never stored,
    so there is genuinely nothing to look up, and saying so is safer than letting
    the model guess.
    """
    norm = normalize_plate(plate)
    if not norm.valid:
        return _answer(
            "That does not look like a valid Indian plate number, so I cannot look it up.",
            follow_ups=["Total stolen vehicles today", "Latest alerts"],
            data={"valid_plate": False},
        )

    key = plate_key(norm.normalized)
    entry = db.execute(
        select(Hotlist).where(
            func.upper(func.replace(Hotlist.plate, " ", "")) == key.upper(),
            Hotlist.status.in_(ACTIVE_HOTLIST_STATES),
        )
    ).scalars().first()

    if entry is None:
        return _answer(
            f"{norm.normalized} is not on the active hot-list, and no record is kept for it. "
            "Non-hot-listed plates are never stored, so there is no sighting history to show.",
            kpis=[{"label": "On hot-list", "value": "No"}],
            follow_ups=["Total stolen vehicles today", "Latest alerts"],
            data={"plate": norm.normalized, "hotlisted": False, "stored": False},
        )

    sightings = db.execute(
        select(func.count()).select_from(Sighting).where(Sighting.hotlist_id == entry.id)
    ).scalar() or 0

    if entry.last_seen_at is None:
        return _answer(
            f"{entry.plate} is on the active hot-list with no sighting recorded yet.",
            kpis=[{"label": "On hot-list", "value": "Yes"}, {"label": "Sightings", "value": 0}],
            links=[{"label": "Open Hotlist", "path": "/hotlist"}],
            follow_ups=["Latest alerts", "Total stolen vehicles today"],
            data={"plate": entry.plate, "hotlisted": True, "sightings": 0},
        )

    lat, lng = round_coord(entry.last_seen_lat), round_coord(entry.last_seen_lng)
    where = f"{lat}, {lng}" if lat is not None and lng is not None else "an unknown location"
    return _answer(
        f"{entry.plate} is on the active hot-list. Last seen {humanize_age(entry.last_seen_at)} ago at {where}, "
        f"{sightings} sighting{'s' if sightings != 1 else ''} in total.",
        kpis=[
            {"label": "On hot-list", "value": "Yes"},
            {"label": "Sightings", "value": int(sightings)},
            {"label": "Last seen", "value": humanize_age(entry.last_seen_at)},
        ],
        links=[{"label": "Open Hotlist", "path": "/hotlist"}],
        follow_ups=["Latest alerts", "Top hot-listed vehicles"],
        data={
            "plate": entry.plate,
            "hotlisted": True,
            "sightings": int(sightings),
            "last_seen_lat": lat,
            "last_seen_lng": lng,
            "last_seen_minutes": common.minutes_since(entry.last_seen_at),
        },
    )


# ------------------------------------------------------------ registry & config


def _spec(answer, description: str, keywords: list[str], label: str, *, question: bool = True) -> dict[str, Any]:
    return {
        "answer": answer,
        "description": description,
        "keywords": keywords,
        "label": label,
        "question": question,
    }


TOOLS: dict[str, dict[str, Any]] = {
    "stolen_today": _spec(
        stolen_today,
        "Active hot-list size plus today's sightings on it.",
        ["total stolen vehicles today", "how many stolen today", "active hotlist", "newly hotlisted"],
        "Total stolen vehicles today",
    ),
    "analytics_today": _spec(
        analytics_today,
        "Detections, hot-list matches, busiest hour and devices today.",
        ["today's analytics", "detections today", "traffic today", "busiest hour", "class mix"],
        "Today's analytics",
    ),
    "latest_alerts": _spec(
        latest_alerts,
        "The most recent hot-list alerts with time and location.",
        ["latest alerts", "recent alerts", "last detections", "recent hits"],
        "Latest alerts",
    ),
    "top_hotlist": _spec(
        top_hotlist,
        "Hot-list vehicles with the most sightings.",
        ["top hotlist", "most wanted", "most sightings", "top stolen vehicles"],
        "Top hot-listed vehicles",
    ),
    "busiest_areas": _spec(
        busiest_areas,
        "Busiest grid cells by vehicle volume.",
        ["busiest areas", "busiest places", "density", "heatmap", "where is traffic"],
        "Busiest areas today",
    ),
    "pending_complaints": _spec(
        pending_complaints,
        "Complaint counts by status and the oldest open one.",
        ["pending complaints", "complaint backlog", "complaints by status", "oldest complaint"],
        "Pending complaints",
    ),
    "ocr_accuracy": _spec(
        ocr_accuracy,
        "Measured OCR plate-read accuracy from the last evaluation.",
        ["ocr accuracy", "model accuracy", "plate read accuracy", "how accurate"],
        "OCR model accuracy",
    ),
    "devices_online": _spec(
        devices_online,
        "Registered cameras and devices, and how many reported recently.",
        ["devices online", "cameras online", "active devices", "how many cameras"],
        "Devices online",
    ),
    # Free-text only: no predefined question, so it never appears as a chip.
    "last_seen": _spec(
        last_seen,
        "Last recorded sighting of a plate, only if it is on the hot-list.",
        ["where was this plate last seen", "last seen plate", "has this plate been seen"],
        "Where was a plate last seen",
        question=False,
    ),
}


def quick_questions(db: Session) -> list[dict[str, Any]]:
    """Predefined questions, with the ones whose data source is absent hidden.

    Hiding beats showing a guaranteed "not available": a chip that never works is
    worse than no chip.
    """
    questions: list[dict[str, Any]] = []
    for tool_id, spec in TOOLS.items():
        if not spec["question"]:
            continue
        if tool_id == "busiest_areas" and not _has_traffic(db):
            continue
        if tool_id == "ocr_accuracy" and not _has_ocr_report():
            continue
        questions.append({"id": tool_id, "label": spec["label"], "keywords": spec["keywords"]})
    return questions


def _has_traffic(db: Session) -> bool:
    try:
        return bool(db.execute(select(func.count()).select_from(TrafficCell).limit(1)).scalar())
    except Exception:
        return False


def _safe_report(loader, path):
    """Run the cached report loader without ever raising on a request.

    A corrupt or half-written report must hide the OCR question, not break the
    whole /chat/config response.
    """
    try:
        return loader(path)
    except Exception:
        return None


def _has_ocr_report() -> bool:
    try:
        from app.api.v1.ocr_metrics import REPORT_PATH
        from app.services.ocr_metrics import load_report

        return _safe_report(load_report, REPORT_PATH) is not None
    except Exception:
        return False


def _fmt_hour(value) -> Optional[str]:
    return value.strftime("%H:00") if value is not None else None


def _pct(value) -> str:
    try:
        return f"{float(value) * 100:.1f}%"
    except (TypeError, ValueError):
        return "n/a"


def is_valid_plate_arg(plate: str) -> bool:
    """Plate argument check, used by the Pydantic schema layer."""
    return bool(plate) and is_valid_plate(plate)
