"""Context compaction: summarize older messages when nearing the context window.

The whole message history is sent on every model call, so a long session will
eventually exceed the model's `context_window`. When the estimated input size
crosses a threshold we summarize the older portion into a single message and
keep a recent, self-consistent tail (so tool_call/tool result pairs are never
split, which the API rejects).
"""

from __future__ import annotations

from typing import Any, Dict, List

from coded.config import ModelConfig

# How many trailing messages to try to preserve verbatim.
_KEEP_RECENT = 6


def estimate_tokens(messages: List[Dict[str, Any]]) -> int:
    """Rough token estimate (~4 chars/token) for when the API omits usage."""
    total = 0
    for m in messages:
        content = m.get("content")
        if isinstance(content, str):
            total += len(content) // 4
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    total += len(part.get("text", "")) // 4
                else:
                    total += 800  # image parts are expensive; rough allowance
        for tc in m.get("tool_calls", []) or []:
            total += len(tc.get("function", {}).get("arguments", "")) // 4 + 10
    return total


def current_input_tokens(session) -> int:
    """Best estimate of the current context size: last real usage, else estimate."""
    if getattr(session, "last_prompt_tokens", 0):
        return session.last_prompt_tokens
    return estimate_tokens(session.messages)


def _transcript(messages: List[Dict[str, Any]]) -> str:
    lines = []
    for m in messages:
        role = m.get("role", "?")
        content = m.get("content")
        if isinstance(content, list):
            content = " ".join(p.get("text", "[image]") for p in content if isinstance(p, dict))
        content = content or ""
        for tc in m.get("tool_calls", []) or []:
            fn = tc.get("function", {})
            content += f"\n[tool call: {fn.get('name')} {fn.get('arguments','')[:200]}]"
        lines.append(f"{role}: {content}".strip())
    return "\n\n".join(lines)


def _summarize(head: List[Dict[str, Any]], llm) -> str:
    messages = [
        {"role": "system", "content":
            "You compress a coding-assistant conversation into a compact briefing. "
            "Preserve: decisions made, files created/edited and how, important facts "
            "learned about the codebase, and any unfinished tasks. Use terse bullet "
            "points. Omit chit-chat."},
        {"role": "user", "content": "Summarize this conversation so far:\n\n" + _transcript(head)},
    ]
    completion = llm.complete(messages, tools=None, stream=False)
    return (completion.content or "").strip() or "(summary unavailable)"


def compaction_threshold(model: ModelConfig, ratio: float) -> int:
    limit = int(model.context_window * ratio)
    if model.max_tokens:
        limit -= model.max_tokens  # leave room for the response
    return max(limit, int(model.context_window * 0.5))


def maybe_compact(session, model: ModelConfig, llm, *, ratio: float = 0.8,
                  keep_recent: int = _KEEP_RECENT, force: bool = False) -> bool:
    """Compact the session in place if it's over threshold. Returns True if it did."""
    messages = session.messages
    if len(messages) < 3:
        return False
    if not force and current_input_tokens(session) < compaction_threshold(model, ratio):
        return False

    system = messages[0]
    body = messages[1:]
    if len(body) <= keep_recent:
        return False

    # Choose a cut point: keep the last `keep_recent`, then move the boundary
    # forward until it lands on a 'user' message so the tail is self-consistent
    # (no assistant tool_calls without their tool results, no orphan tool msgs).
    cut = len(body) - keep_recent
    while cut < len(body) and body[cut].get("role") != "user":
        cut += 1
    if cut >= len(body):
        cut = len(body)  # summarize everything; keep nothing partial
    head, tail = body[:cut], body[cut:]
    if not head:
        return False

    summary = _summarize(head, llm)
    summary_msg = {
        "role": "user",
        "content": "[Earlier conversation summarized to save context]\n" + summary,
    }
    session.messages = [system, summary_msg] + tail
    session.last_prompt_tokens = 0  # force re-estimate next time
    session.compactions = getattr(session, "compactions", 0) + 1
    return True
