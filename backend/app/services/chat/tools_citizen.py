"""Citizen toolset (CITIZEN only): own-complaint lookups plus fixed-topic FAQ.

Two hard rules shape this module:

1. **Isolation.** ``my_complaints`` filters on ``Complaint.user_id == user.id``, the
   same predicate ``GET /complaints/mine`` uses. There is no argument a model can
   pass that widens that scope, so one citizen can never read another's rows.
2. **No hot-list answers.** A citizen can never learn whether a plate is stolen,
   nor see a grain of police analytics. The prompt injection test in
   backend/tests/test_chat.py asserts this from the outside.

The FAQ answers are fixed strings grounded in what this codebase actually does
(complaint intake, proof rules, the status flow). They deliberately promise
nothing about timings or outcomes.
"""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Complaint, ComplaintStatus, User
from app.services.chat import common
from app.services.chat.common import humanize_age

# Only the caller's own rows, newest first. Same ordering as /complaints/mine.
MAX_OWN_COMPLAINTS = common.MAX_ROWS

FAQ_TOPICS = (
    "report_steps",
    "required_proof",
    "after_verify",
    "privacy",
    "emergency",
    "complaint_status",
)


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


# ------------------------------------------------------------------- overviews


def my_complaints(db: Session, user: User) -> dict[str, Any]:
    """Counts by status plus each of the caller's own complaints.

    Scoped to ``user.id`` with no override, mirroring ``complaints.my_complaints``.
    """
    rows = list(
        db.execute(
            select(Complaint).where(Complaint.user_id == user.id).order_by(Complaint.created_at.desc())
        ).scalars()
    )

    counts: dict[str, int] = {status.value: 0 for status in ComplaintStatus}
    for row in rows:
        counts[row.status.value] = counts.get(row.status.value, 0) + 1

    if not rows:
        return _answer(
            "You have not filed any complaints yet. You can report a stolen vehicle from the Report page.",
            kpis=[{"label": "Your complaints", "value": 0}],
            links=[{"label": "Report a stolen vehicle", "path": "/complaints/new"}],
            follow_ups=["How do I report a stolen vehicle?", "What proof do I need to upload?"],
            data={"count": 0, "by_status": counts, "complaints": []},
        )

    items = [
        {
            "plate": row.plate,
            "complaint_type": row.complaint_type,
            "status": row.status.value,
            "age": humanize_age(row.created_at),
        }
        for row in rows[:MAX_OWN_COMPLAINTS]
    ]

    lines = [
        f"{item['plate']} ({item['complaint_type']}, {item['status'].replace('_', ' ').lower()}, {item['age']} ago)"
        for item in items
    ]
    open_total = counts[ComplaintStatus.PENDING.value] + counts[ComplaintStatus.UNDER_REVIEW.value]

    return _answer(
        f"You have {len(rows)} complaint{'s' if len(rows) != 1 else ''}. "
        + "; ".join(lines)
        + ".",
        kpis=[
            {"label": "Total", "value": len(rows)},
            {"label": "Awaiting review", "value": open_total},
            {"label": "Hot-listed", "value": counts[ComplaintStatus.HOTLISTED.value]},
        ],
        links=[{"label": "Open My complaints", "path": "/complaints"}],
        follow_ups=["What happens after police verify my complaint?", "What proof do I need to upload?"],
        data={"count": len(rows), "by_status": counts, "complaints": items},
    )


# ------------------------------------------------------------------------- FAQ

_FAQ: dict[str, dict[str, Any]] = {
    "report_steps": {
        "text": (
            "To report a stolen vehicle: open Report, enter the registration number, pick the complaint type, "
            "add an optional description, attach proof, then submit. Your complaint starts as PENDING and "
            "police review it before anything is shared with volunteer cameras."
        ),
        "links": [{"label": "Report a stolen vehicle", "path": "/complaints/new"}],
        "follow_ups": ["What proof do I need to upload?", "What happens after police verify my complaint?"],
    },
    "required_proof": {
        "text": (
            "Proof is optional when you file, but it is what lets police verify quickly. Accepted files are "
            "JPG, JPEG, PNG and PDF, up to the upload size limit shown on the form. An FIR copy or a photo of "
            "the vehicle documents works well."
        ),
        "links": [{"label": "Report a stolen vehicle", "path": "/complaints/new"}],
        "follow_ups": ["How do I report a stolen vehicle?", "What happens after police verify my complaint?"],
    },
    "after_verify": {
        "text": (
            "Police check your complaint. If they verify it, the vehicle goes on the hot-list and volunteer "
            "cameras watch for it; if they reject it, the complaint is marked REJECTED and nothing is watched. "
            "A verified complaint then moves through VERIFIED to HOTLISTED, and finally CLOSED when it is "
            "resolved. I cannot promise how long any step takes."
        ),
        "links": [{"label": "Open My complaints", "path": "/complaints"}],
        "follow_ups": ["What is the status of my complaints?", "How is my privacy protected?"],
    },
    "privacy": {
        "text": (
            "Your complaint is visible to you and to police who review it. Plate photos and locations are "
            "encrypted in transit. Volunteer cameras only ever report a match against the police hot-list, and "
            "images of bystanders or the general public are not uploaded - only a matched plate reading is "
            "recorded."
        ),
        "links": [],
        "follow_ups": ["What happens after police verify my complaint?", "What proof do I need to upload?"],
    },
    "emergency": {
        "text": (
            "If this is an emergency or a crime in progress, call 112 now - that is India's national emergency "
            "number. This app is not monitored for emergencies and cannot dispatch help."
        ),
        "links": [],
        "follow_ups": ["How do I report a stolen vehicle?"],
    },
    "complaint_status": {
        "text": (
            "A complaint moves through PENDING, then UNDER_REVIEW, then either REJECTED or on to VERIFIED and "
            "HOTLISTED, and finally CLOSED once it is resolved."
        ),
        "links": [{"label": "Open My complaints", "path": "/complaints"}],
        "follow_ups": ["What is the status of my complaints?", "What happens after police verify my complaint?"],
    },
}


def faq(topic: str) -> dict[str, Any]:
    """Answer one fixed topic. An unknown topic lists what is available."""
    key = (topic or "").strip().lower()
    entry = _FAQ.get(key)
    if entry is None:
        return _answer(
            "I can explain: reporting steps, required proof, what happens after verification, privacy, "
            "complaint status, and emergencies.",
            follow_ups=[q["label"] for q in CITIZEN_QUESTIONS],
            data={"topic": None, "available_topics": list(FAQ_TOPICS)},
        )
    return _answer(
        entry["text"],
        links=entry.get("links", []),
        follow_ups=entry.get("follow_ups", []),
        data={"topic": key},
    )


# ------------------------------------------------------------ registry & config

CITIZEN_QUESTIONS: list[dict[str, Any]] = [
    {
        "id": "how_to_report",
        "label": "How do I report a stolen vehicle?",
        "tool": "faq",
        "params": {"topic": "report_steps"},
        "keywords": ["how do i report", "report a stolen vehicle", "file a complaint", "register a complaint"],
    },
    {
        "id": "my_complaints",
        "label": "What is the status of my complaints?",
        "tool": "my_complaints",
        "params": {},
        "keywords": ["my complaints", "status of my complaint", "complaint status", "did my complaint go through"],
    },
    {
        "id": "required_proof",
        "label": "What proof do I need to upload?",
        "tool": "faq",
        "params": {"topic": "required_proof"},
        "keywords": ["what proof", "which documents", "upload proof", "fir copy", "what file types"],
    },
    {
        "id": "after_verify",
        "label": "What happens after police verify my complaint?",
        "tool": "faq",
        "params": {"topic": "after_verify"},
        "keywords": ["after verification", "once police verify", "what happens next", "verified complaint"],
    },
    {
        "id": "privacy_info",
        "label": "How is my privacy protected?",
        "tool": "faq",
        "params": {"topic": "privacy"},
        "keywords": ["privacy", "who can see my complaint", "is my data safe", "data protection"],
    },
    {
        "id": "emergency",
        "label": "Emergency or crime in progress",
        "tool": "faq",
        "params": {"topic": "emergency"},
        "keywords": ["emergency", "crime in progress", "urgent", "police emergency number", "help now"],
    },
]

TOOLS: dict[str, dict[str, Any]] = {
    "my_complaints": {
        "answer": my_complaints,
        "description": "The caller's own complaints with counts by status.",
        "needs_user": True,
    },
    "faq": {
        "answer": faq,
        "description": "Fixed help topics about reporting, proof, verification, privacy and emergencies.",
        "needs_user": False,
    },
}


def quick_questions(db: Session, user: User) -> list[dict[str, Any]]:
    """Citizen chips. Every tool here needs no external data source, so none are hidden."""
    return [
        {"id": q["id"], "label": q["label"], "keywords": q["keywords"]}
        for q in CITIZEN_QUESTIONS
    ]


def question_by_id(question_id: str) -> Optional[dict[str, Any]]:
    for question in CITIZEN_QUESTIONS:
        if question["id"] == question_id:
            return question
    return None
