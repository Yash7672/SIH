"""Verify the Groq setup for the RAKSHAK assistant. Run: python scripts/groq_check.py

Model ids change often and an account's entitlement decides what is actually
callable, so nothing here trusts a hard-coded id. This script:

  1. reads GROQ_API_KEY from the repo ``.env`` (it is never printed);
  2. lists the models this key can call;
  3. confirms the configured ``GROQ_MODEL`` exists, and otherwise picks a current
     production model suited to fast routing plus a larger fallback;
  4. sends one tiny routed request and prints latency and token counts.

If there is no key it says so and exits 0 - that is a supported mode (the
assistant then answers from the predefined questions only), not a failure.

Usage:
    python scripts/groq_check.py
    python scripts/groq_check.py --model openai/gpt-oss-20b
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "backend"))

# A small fast model routes best; the larger one is the fallback on 404/429/5xx.
PREFERRED_MODEL = "openai/gpt-oss-20b"
PREFERRED_FALLBACK = "openai/gpt-oss-120b"

# Ids that can never be a chat router: speech, voice cloning, classifiers.
SKIP_MARKERS = ("whisper", "orpheus", "guard", "tts")

TIMEOUT_SECONDS = 8.0

# One trivial tool: enough to prove function calling works, cheap enough to be
# safe on a free tier.
PROBE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "total_stolen_today",
            "description": "Count of stolen vehicles today",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    }
]


def _read_env(name: str) -> str:
    """Read one key from the repo .env without disturbing the process env."""
    for candidate in (REPO_ROOT / ".env", REPO_ROOT / "backend" / ".env"):
        if not candidate.is_file():
            continue
        for line in candidate.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line.startswith(f"{name}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return os.environ.get(name, "")


def _chat_capable(model_ids: list[str]) -> list[str]:
    return [m for m in model_ids if not any(skip in m.lower() for skip in SKIP_MARKERS)]


def _pick(model_ids: list[str], preferred: str) -> str:
    if preferred in model_ids:
        return preferred
    candidates = _chat_capable(model_ids)
    return candidates[0] if candidates else ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the Groq configuration for RAKSHAK chat.")
    parser.add_argument("--model", default="", help="Override GROQ_MODEL for this check.")
    parser.add_argument("--fallback", default="", help="Override GROQ_FALLBACK_MODEL.")
    args = parser.parse_args(argv)

    key = _read_env("GROQ_API_KEY")
    if not key:
        print("No GROQ_API_KEY found in .env.")
        print("The assistant runs in quick-only mode: predefined questions still work,")
        print("and free text falls back to fuzzy matching. Set GROQ_API_KEY to enable AI routing.")
        print("Get a key at https://console.groq.com/keys")
        return 0

    # Never print the key itself - only that it is present and how long it is.
    print(f"GROQ_API_KEY found (length {len(key)}, prefix {key[:4]}...).")

    try:
        from groq import AsyncGroq
    except ImportError:
        print("The 'groq' package is not installed. Run: pip install -r backend/requirements.txt")
        return 1

    import asyncio

    async def run() -> int:
        client = AsyncGroq(api_key=key, timeout=TIMEOUT_SECONDS, max_retries=0)

        try:
            listing = await client.models.list()
        except Exception as exc:  # noqa: BLE001
            print(f"Could not reach Groq: {type(exc).__name__}: {exc}")
            print("Check the network and that the key is valid. Quick-only mode still works.")
            return 1

        model_ids = sorted(getattr(m, "id", "") for m in getattr(listing, "data", []) if getattr(m, "id", ""))
        print(f"\n{len(model_ids)} models available to this key:")
        for model_id in model_ids:
            print(f"  - {model_id}")

        configured = (args.model or _read_env("GROQ_MODEL")).strip()
        fallback = (args.fallback or _read_env("GROQ_FALLBACK_MODEL")).strip()

        if configured and configured not in model_ids:
            print(f"\nGROQ_MODEL '{configured}' is NOT available to this key. Auto-selecting instead.")
            configured = ""
        if fallback and fallback not in model_ids:
            print(f"GROQ_FALLBACK_MODEL '{fallback}' is NOT available to this key. Auto-selecting instead.")
            fallback = ""

        model = configured or _pick(model_ids, PREFERRED_MODEL)
        if not model:
            print("\nNo chat-capable model is available to this key. Quick-only mode it is.")
            return 1

        if not fallback or fallback == model:
            fallback = next(
                (m for m in (PREFERRED_FALLBACK, PREFERRED_MODEL) if m in model_ids and m != model),
                model,
            )

        print(f"\nRouting model : {model}")
        print(f"Fallback model: {fallback}")

        # One tiny tool-calling request: this is what proves tool use really works.
        messages = [
            {"role": "system", "content": "Route the question to a tool. Do not answer from memory."},
            {"role": "user", "content": "How many stolen vehicles today?"},
        ]

        try:
            started = time.perf_counter()
            response = await client.chat.completions.create(
                model=model,
                messages=messages,
                tools=PROBE_TOOLS,
                tool_choice="auto",
                max_tokens=120,
                temperature=0.1,
            )
            latency_ms = int((time.perf_counter() - started) * 1000)
        except Exception as exc:  # noqa: BLE001
            print(f"\nTool-calling check FAILED: {type(exc).__name__}: {exc}")
            print("The assistant will fall back to quick answers until this is resolved.")
            return 1

        message = response.choices[0].message
        calls = getattr(message, "tool_calls", None) or []
        usage = getattr(response, "usage", None)
        prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)

        if not calls:
            print("\nThe model did not call a tool. Tool calling may be unsupported for this id.")
            return 1

        names = [getattr(getattr(c, "function", None), "name", "?") for c in calls]
        print(f"\nTool-calling check OK: called {names}")
        print(f"Latency: {latency_ms} ms | tokens: prompt {prompt_tokens}, completion {completion_tokens}")
        print(f"Estimated per-request input tokens: {prompt_tokens} (keep the system prompt small)")
        print("\nGroq is configured correctly. Set these in .env to pin the choice:")
        print(f"  GROQ_MODEL={model}")
        print(f"  GROQ_FALLBACK_MODEL={fallback}")
        return 0

    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
