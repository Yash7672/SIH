"""AI assistant endpoints. One family; the JWT role decides everything.

    GET  /api/v1/chat/config   - quick questions for the caller's role + llm flag
    POST /api/v1/chat/quick    - run a predefined question (no LLM at all)
    POST /api/v1/chat/message  - free text: Groq routes, backend executes

Access is derived from the token, never from the request body:

    CITIZEN                 -> citizen toolset
    COP, ADMIN              -> police toolset
    VOLUNTEER               -> 403
    no/invalid token        -> 401

Nothing here writes to the database, and the server stores no message text.
"""

from __future__ import annotations

import time
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.config import settings
from app.core.logging import get_logger
from app.db.session import get_db
from app.models import Role, User
from app.services.audit_service import log_action
from app.services.chat import router as chat_router
from app.services.chat import quick as quick_mod
from app.services.chat.llm_groq import groq_router

logger = get_logger(__name__)

router = APIRouter()


class QuickRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=60)


class HistoryTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: str = Field(max_length=20)
    content: str = Field(max_length=500)

    @field_validator("role")
    @classmethod
    def _known_role(cls, value: str) -> str:
        # Anything else (system/tool) is not a turn the client may author.
        if value not in ("user", "assistant"):
            raise ValueError("history role must be 'user' or 'assistant'")
        return value


class MessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=quick_mod.MAX_MESSAGE_CHARS)
    history: list[HistoryTurn] = Field(default_factory=list, max_length=8)


def require_chat_user(user: User = Depends(get_current_user)) -> User:
    """401 handled by get_current_user; 403 for roles with no toolset."""
    if chat_router.toolset_for(user.role) is None:
        raise HTTPException(status_code=403, detail="The assistant is not available for this account type")
    return user


def _ensure_enabled() -> None:
    if not settings.chat_enabled:
        raise HTTPException(status_code=503, detail="The assistant is disabled")


def _llm_available() -> bool:
    return bool(settings.chat_llm_ready and groq_router.ready)


def _enforce_rate_limit(user: User) -> None:
    allowed, retry_after, scope = quick_mod.check_rate_limit(user.id)
    if allowed:
        return
    message = quick_mod.rate_limit_message(scope, retry_after)
    raise HTTPException(
        status_code=429,
        detail=message,
        headers={"Retry-After": str(max(retry_after, 1))},
    )


def _log_query(user: User, tool: Optional[str], latency_ms: int, usage: Optional[dict], outcome: str) -> None:
    """Log shape only: role, tool names, latency, token counts, outcome.

    The message text and the answer are deliberately absent - a chat log that
    stores what people asked is a privacy problem, not a debugging aid.
    """
    logger.info(
        "chat role=%s tool=%s latency_ms=%d prompt_tokens=%d completion_tokens=%d outcome=%s",
        user.role.value,
        tool or "-",
        latency_ms,
        int((usage or {}).get("prompt_tokens", 0) or 0),
        int((usage or {}).get("completion_tokens", 0) or 0),
        outcome,
    )


def _audit(user: User, tool: Optional[str], outcome: str) -> None:
    """Audit row for police queries: tool names only, never the text."""
    if user.role not in (Role.COP, Role.ADMIN):
        return
    try:
        db = next(get_db())
        try:
            log_action(
                db,
                user.id,
                "chat_query",
                "chat",
                tool or None,
                {"tools": [tool] if tool else [], "outcome": outcome, "role": user.role.value},
            )
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001 - auditing must not break the answer
        logger.warning("Chat audit row skipped: %s", type(exc).__name__)


@router.get("/config")
def chat_config(
    response: Response,
    user: User = Depends(require_chat_user),
    db: Session = Depends(get_db),
):
    """Quick questions for this role and whether the LLM is usable right now."""
    if not settings.chat_enabled:
        return {"enabled": False, "llm_enabled": False, "questions": [], "provider": "groq"}
    # Role-specific payloads must not be cached by a shared proxy.
    response.headers["Cache-Control"] = "private, no-store"
    return {
        "enabled": True,
        "llm_enabled": _llm_available(),
        "provider": "groq",
        "questions": chat_router.quick_questions(user.role, db, user),
    }


@router.post("/quick")
async def chat_quick(
    payload: QuickRequest,
    response: Response,
    user: User = Depends(require_chat_user),
    db: Session = Depends(get_db),
):
    """Run a predefined question. Deterministic - no model is involved."""
    _ensure_enabled()
    _enforce_rate_limit(user)

    started = time.perf_counter()
    answer = chat_router.run_quick(user.role, db, user, payload.id)
    if answer is None:
        raise HTTPException(status_code=404, detail="Unknown question for this account")

    latency_ms = int((time.perf_counter() - started) * 1000)
    _log_query(user, payload.id, latency_ms, None, "quick")
    _audit(user, payload.id, "quick")

    response.headers["Cache-Control"] = "private, no-store"
    return {
        "text": answer["text"],
        "kpis": answer["kpis"],
        "links": answer["links"],
        "follow_ups": answer["follow_ups"],
        "kind": "quick",
        "used_llm": False,
        "tool": payload.id,
    }


@router.post("/message")
async def chat_message(
    payload: MessageRequest,
    response: Response,
    user: User = Depends(require_chat_user),
    db: Session = Depends(get_db),
):
    """Free text: Groq picks an approved tool, the backend answers from real data."""
    _ensure_enabled()
    _enforce_rate_limit(user)

    started = time.perf_counter()
    result = await chat_router.run_message(
        user.role,
        db,
        user,
        quick_mod.clean_message(payload.message),
        quick_mod.sanitise_history([t.model_dump() for t in payload.history]),
    )
    latency_ms = int((time.perf_counter() - started) * 1000)

    _log_query(user, result.get("tool"), latency_ms, result.get("usage"), result.get("kind", "answer"))
    _audit(user, result.get("tool"), result.get("kind", "answer"))

    response.headers["Cache-Control"] = "private, no-store"
    return {
        "text": result["text"],
        "kpis": result["kpis"],
        "links": result["links"],
        "follow_ups": result["follow_ups"],
        # Offered back to the client so an unmatched question can be answered by
        # showing the chips again instead of dead-ending.
        "questions": result.get("questions", []),
        "kind": result["kind"],
        "used_llm": result["used_llm"],
        "tool": result["tool"],
        "llm_available": _llm_available(),
    }
