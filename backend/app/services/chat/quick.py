"""Per-user chat limits and history sanitising.

Kept out of the endpoint so the rules can be unit-tested without HTTP, and out of
``llm_groq`` because a rate limit applies even when the LLM is never called: quick
questions are deterministic but still worth capping.
"""

from __future__ import annotations

from typing import Any, Optional
from uuid import UUID

from app.core.config import settings
from app.services.cache import cache_service

MINUTE_WINDOW = 60
DAY_WINDOW = 60 * 60 * 24


def _limits() -> tuple[int, int]:
    return max(0, int(settings.CHAT_RATE_PER_MIN)), max(0, int(settings.CHAT_RATE_PER_DAY))


def check_rate_limit(user_id: UUID) -> tuple[bool, int, str]:
    """(allowed, retry_after_seconds, scope).

    Checked per user rather than per IP: a shared campus NAT would otherwise let
    one person exhaust everyone's budget.
    """
    per_min, per_day = _limits()

    if per_min:
        allowed, retry = cache_service.rate_limit(f"chat:min:{user_id}", per_min, MINUTE_WINDOW)
        if not allowed:
            return False, retry, "minute"

    if per_day:
        allowed, retry = cache_service.rate_limit(f"chat:day:{user_id}", per_day, DAY_WINDOW)
        if not allowed:
            return False, retry, "day"

    return True, 0, ""


def rate_limit_message(scope: str, retry_after: int) -> str:
    """A message a person can act on, never a bare 429."""
    if scope == "minute":
        return f"Rate limit reached, try again in {max(retry_after, 1)} s."
    return (
        "You have reached today's assistant limit. "
        "Quick questions still work, or try again tomorrow."
    )


def sanitise_history(history: Optional[list[Any]]) -> list[dict[str, str]]:
    """Keep only well-formed user/assistant turns from the client.

    A client may not smuggle in a ``system`` or ``tool`` role - that is the
    prompt-injection path the tests assert is closed.
    """
    if not history:
        return []
    clean: list[dict[str, str]] = []
    for turn in history:
        if not isinstance(turn, dict):
            continue
        role = turn.get("role")
        content = turn.get("content")
        if role not in ("user", "assistant"):
            continue
        if not isinstance(content, str) or not content.strip():
            continue
        clean.append({"role": role, "content": content[:500]})
    return clean[-6:]


MAX_MESSAGE_CHARS = 500


def clean_message(message: str) -> str:
    """Trim and bound the user message (validated again by Pydantic)."""
    return (message or "").strip()[:MAX_MESSAGE_CHARS]
