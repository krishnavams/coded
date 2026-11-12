"""What fills the context window (/context) and a readable export (/export).

Both work from the agent's live conversation; neither calls the model. Token
counts are estimates (about four characters a token, like compaction uses),
shown next to the real figure the endpoint reported for the last request.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from coded.compaction import compaction_threshold, estimate_tokens


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content
                         if isinstance(p, dict) and p.get("type") == "text")
    return ""


def context_breakdown(agent: Any) -> Dict[str, Any]:
    """Estimated tokens per part of the request the next model call sends."""
    msgs = agent.session.messages
    parts: List[Tuple[str, int]] = []
    system = [m for m in msgs if m.get("role") == "system"]
    parts.append(("System prompt", estimate_tokens(system)))
    schema = None
    try:
        schema = agent._tool_schema()
    except Exception:  # noqa: BLE001 - a display helper must not fail
        schema = None
    parts.append((f"Tool definitions ({len(schema or [])})",
                  len(json.dumps(schema)) // 4 if schema else 0))
    by_role = {"user": 0, "assistant": 0, "tool": 0}
    images = 0
    for m in msgs:
        role = m.get("role")
        if role in by_role:
            by_role[role] += estimate_tokens([m])
        if isinstance(m.get("content"), list):
            images += sum(1 for p in m["content"]
                          if isinstance(p, dict) and p.get("type") == "image_url")
    parts.append(("Your messages" + (f" ({images} image(s))" if images else ""), by_role["user"]))
    parts.append(("Model replies and tool calls", by_role["assistant"]))
    parts.append(("Tool results", by_role["tool"]))
    model = getattr(agent, "_sized_model", None) or agent.model
    window = model.context_window
    used = sum(n for _, n in parts)
    return {
        "parts": parts,
        "estimated": used,
        "reported": getattr(agent.session, "last_prompt_tokens", 0),
        "window": window,
        "compact_at": compaction_threshold(model, getattr(agent.config, "compact_ratio", 0.8)),
        "model": model.name,
        "messages": len(msgs),
    }


def render_context(info: Dict[str, Any], width: int = 30) -> str:
    """Plain-text table with a bar per part (rich markup-free)."""
    window = max(info["window"], 1)
    lines = [f"Context for {info['model']}: window {info['window']:,} tokens, "
             f"compacts at {info['compact_at']:,}."]
    name_w = max(len(n) for n, _ in info["parts"])
    for name, n in info["parts"]:
        cells = round(width * n / window)
        bar = "▰" * cells + "▱" * (width - cells)
        lines.append(f"  {name:<{name_w}}  {bar}  {n:>8,}  {n / window:6.1%}")
    est = info["estimated"]
    lines.append(f"  {'Total (estimated)':<{name_w}}  {'':{width}}  {est:>8,}  {est / window:6.1%}")
    if info["reported"]:
        lines.append(f"  The endpoint counted {info['reported']:,} input tokens on the last request.")
    lines.append(f"  {info['messages']} messages. /compact summarizes old ones; "
                 f"/rewind drops recent ones.")
    return "\n".join(lines)


def export_markdown(agent: Any, *, result_chars: int = 4000) -> str:
    """The conversation as Markdown: requests, answers, tool calls and (cut)
    results. The system prompt is left out."""
    out = [f"# coded session — {time.strftime('%Y-%m-%d %H:%M')}", "",
           f"Model: `{agent.model.name}` · Directory: `{agent.cwd}`", ""]
    names: Dict[str, str] = {}
    for m in agent.session.messages:
        role = m.get("role")
        if role == "system":
            continue
        if role == "user":
            text = _text_of(m.get("content")).strip()
            out += ["## You", "", text, ""]
        elif role == "assistant":
            text = (m.get("content") or "").strip()
            if text:
                out += ["## coded", "", text, ""]
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function", {})
                names[tc.get("id", "")] = fn.get("name", "")
                args = fn.get("arguments", "")
                try:
                    args = json.dumps(json.loads(args), indent=2, ensure_ascii=False)
                except (ValueError, TypeError):
                    pass
                out += [f"**Tool call: `{fn.get('name', '')}`**", "", "```json", args, "```", ""]
        elif role == "tool":
            body = str(m.get("content") or "")
            if len(body) > result_chars:
                body = body[:result_chars] + f"\n… ({len(body) - result_chars:,} more characters)"
            name = names.get(m.get("tool_call_id", ""), "tool")
            fence = "````" if "```" in body else "```"
            out += [f"<details><summary>Result: {name}</summary>", "", fence, body, fence,
                    "", "</details>", ""]
    return "\n".join(out).rstrip() + "\n"


def write_export(agent: Any, path: Optional[str] = None) -> Path:
    target = Path(path).expanduser() if path else Path(
        agent.cwd, f"coded-session-{time.strftime('%Y%m%d-%H%M%S')}.md")
    if not target.is_absolute():
        target = Path(agent.cwd) / target
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(export_markdown(agent), encoding="utf-8")
    return target
