"""`coded models --speed`: how fast each configured model really is, here.

Speed claims from providers are measured on their best day with a one-line
prompt. What matters for coded is the request a turn actually sends — the
system prompt and every tool definition, often 5-10k tokens before the user's
words — through this machine's network to this gateway. So the default request
is exactly that; `minimal` drops it to see the raw model.

Reported per model:
  first token   time until the first text or reasoning arrives (reading the
                prompt + queueing: what you wait before anything appears)
  total         time for the whole answer
  speed         output tokens per second after the first token
  prompt        tokens sent (from the server's usage, else estimated)
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, replace
from typing import Any, Callable, List, Optional

_QUESTION = ("In two or three sentences, explain what a hash map is and when you would "
             "use one. Do not use any tools.")


@dataclass
class SpeedResult:
    name: str
    ok: bool
    first_token: Optional[float] = None   # seconds
    total: Optional[float] = None         # seconds
    output_tokens: int = 0
    prompt_tokens: int = 0
    tokens_per_second: Optional[float] = None
    estimated: bool = False               # token counts estimated, not reported
    error: str = ""


def request_for(model: Any, config: Any, cwd: str, minimal: bool = False):
    """(messages, tools) as a real first turn would send them to `model`."""
    if minimal:
        return [{"role": "user", "content": _QUESTION}], None
    from coded.agent import create_agent
    from coded.permissions import PermissionManager

    agent = create_agent(model=model, config=config, cwd=cwd,
                         permissions=PermissionManager(), stream=False, verbose=False)
    system = agent.session.messages[0]
    return [system, {"role": "user", "content": _QUESTION}], agent._tool_schema()


def measure(model: Any, messages: list, tools: Optional[list], runs: int = 1,
            clock: Callable[[], float] = time.perf_counter) -> SpeedResult:
    """Time `runs` streamed requests to `model` (no retries, no fallback) and
    report the median of each figure."""
    from coded.compaction import estimate_tokens
    from coded.llm import LLMClient, explain_failure

    client = LLMClient(replace(model, max_retries=0), fallbacks=[], last_resort=[])
    firsts: List[float] = []
    totals: List[float] = []
    rates: List[float] = []
    out_tokens: List[int] = []
    prompt_tokens = 0
    estimated = False
    for _ in range(max(1, runs)):
        first: List[float] = []
        start = clock()

        def seen(_text: str) -> None:
            if not first:
                first.append(clock())

        try:
            completion = client.complete(messages, tools, stream=True,
                                         on_text=seen, on_reasoning=seen)
        except Exception as exc:  # noqa: BLE001 - report it, keep measuring the rest
            return SpeedResult(model.name, False, error=explain_failure(exc).splitlines()[0])
        end = clock()
        first_at = first[0] if first else end
        usage = completion.usage
        produced = usage.completion_tokens
        if not produced:
            produced = max(1, len(completion.content + completion.reasoning) // 4)
            estimated = True
        prompt_tokens = usage.prompt_tokens or prompt_tokens
        if not usage.prompt_tokens:
            prompt_tokens = estimate_tokens(messages) + (
                sum(len(str(t)) for t in tools) // 4 if tools else 0)
            estimated = True
        firsts.append(first_at - start)
        totals.append(end - start)
        out_tokens.append(produced)
        gen = end - first_at
        # An answer delivered in one burst (a buffering gateway) has no
        # meaningful rate; say nothing rather than print a huge number.
        if gen >= 0.05 and produced > 1:
            rates.append(produced / gen)
    return SpeedResult(
        name=model.name, ok=True,
        first_token=statistics.median(firsts), total=statistics.median(totals),
        output_tokens=int(statistics.median(out_tokens)), prompt_tokens=prompt_tokens,
        tokens_per_second=statistics.median(rates) if rates else None, estimated=estimated)


def advice(results: List[SpeedResult]) -> List[str]:
    """A few plain sentences on what the numbers suggest."""
    ok = [r for r in results if r.ok and r.first_token is not None]
    lines: List[str] = []
    if len(ok) >= 2:
        quick = min(ok, key=lambda r: r.first_token)
        fast = max((r for r in ok if r.tokens_per_second), key=lambda r: r.tokens_per_second,
                   default=None)
        lines.append(f"Quickest to start answering: {quick.name} ({quick.first_token:.1f}s).")
        if fast is not None and fast is not quick:
            lines.append(f"Fastest once it starts: {fast.name} "
                         f"({fast.tokens_per_second:.0f} tok/s).")
        lines.append("For short questions the first-token time matters most; for long "
                     "answers and edits, the speed. Point routing's `simple` and `hard` levels accordingly.")
    slow = [r for r in ok if r.first_token and r.first_token > 5 and r.prompt_tokens > 4000]
    if slow:
        lines.append("Slow to start with a large prompt: " + ", ".join(r.name for r in slow)
                     + ". Try `disabled_tools`, the compact prompt profile, or a server with "
                       "prefix caching.")
    return lines
