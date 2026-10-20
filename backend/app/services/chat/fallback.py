"""Deterministic fallback when Groq is unavailable, rate-limited or broken.

The assistant must never dead-end. When the model cannot be reached, free text is
matched against the role's predefined questions and the best match is executed
deterministically - the same code path a chip click takes, so the answer is just
as trustworthy. If nothing is close enough the caller gets the list of questions
instead of an error, and a raw provider error never reaches the user.

rapidfuzz is used when present and difflib otherwise: both are already in the
environment's dependency graph, and neither is imported at module scope so this
file cannot be the reason the API fails to boot.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional

# Below this, a "match" is noise: a confident wrong answer is worse than saying
# "here are the questions I can answer".
MATCH_THRESHOLD = 62.0


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if len(t) > 2}


def _token_score(a: str, b: str) -> float:
    """Jaccard-ish overlap, 0..100, used to break fuzzy ties."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return 100.0 * len(ta & tb) / len(ta | tb)


def _ratio(a: str, b: str) -> float:
    """Best available string similarity, 0..100."""
    try:  # rapidfuzz is a transitive dep in practice; prefer it when installed
        from rapidfuzz import fuzz

        return float(fuzz.token_set_ratio(a, b))
    except Exception:
        from difflib import SequenceMatcher

        return 100.0 * SequenceMatcher(None, a.lower(), b.lower()).ratio()


def score_match(message: str, question: dict) -> float:
    """Similarity of a free-text message to one predefined question.

    Both the visible label and every keyword the question declares are compared,
    and the best score wins: "how many stolen today" should reach the same place
    as "Total stolen vehicles today".
    """
    candidates: list[str] = [question.get("label", "")]
    candidates.extend(question.get("keywords", []) or [])
    if question.get("text"):
        candidates.append(question["text"])

    best = 0.0
    for candidate in candidates:
        if not candidate:
            continue
        fuzzy = _ratio(message, candidate)
        overlap = _token_score(message, candidate)
        # Weighted so a shared distinctive word ("hotlist", "complaints") beats
        # a merely similar-looking string.
        best = max(best, 0.7 * fuzzy + 0.3 * overlap)
    return best


def best_question(message: str, questions: Iterable[dict]) -> Optional[dict]:
    """The closest predefined question, or None when nothing is close enough."""
    best: Optional[dict] = None
    best_score = 0.0
    for question in questions:
        score = score_match(message, question)
        if score > best_score:
            best, best_score = question, score
    return best if best_score >= MATCH_THRESHOLD else None


def suggestion_text(questions: Iterable[dict]) -> str:
    """A friendly "here is what I can answer" line; never an error."""
    labels = [q.get("label", "") for q in questions if q.get("label")]
    if not labels:
        return (
            "I could not match that to a question I can answer. "
            "Try asking about complaints, today's thefts or the hot-list."
        )
    preview = "; ".join(labels[:6])
    return (
        "I could not match that to a question I can answer confidently. "
        f"Try one of these: {preview}."
    )
