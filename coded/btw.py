"""/btw: a quick side question that does not disturb the work in progress.

The question is answered by one model call over a *copy* of the conversation
(no tools run), so it can be asked while a turn is running, and neither the
question nor the answer is added to the session: the agent's context and task
stay exactly as they were. The usage is still counted.
"""

from __future__ import annotations

import threading
from typing import Any, List

from coded.sessions_store import close_tool_calls

BTW_INSTRUCTION = (
    "[Side question from the user (/btw). Answer it briefly and directly from what "
    "this conversation already shows. Do not call tools, do not continue or change "
    "the current task, and do not mention this note. If the answer needs work you "
    "have not done, say so in one line.]"
)

_USAGE_LOCK = threading.Lock()


def side_messages(messages: List[dict], question: str) -> List[dict]:
    """The conversation as it stands, made valid, with the side question last."""
    msgs = [dict(m) for m in messages]
    # A step in flight: the model asked for tools whose results are not back.
    while msgs and msgs[-1].get("role") == "assistant" and msgs[-1].get("tool_calls"):
        msgs.pop()
    msgs = close_tool_calls(msgs)
    msgs.append({"role": "user", "content": f"{BTW_INSTRUCTION}\n\n{question}"})
    return msgs


def ask(agent: Any, question: str) -> str:
    """Answer `question` from `agent`'s current context; never touches its session."""
    question = question.strip()
    if not question:
        return "Usage: /btw <question> — a quick side question; the current work goes on."
    llm = agent._make_llm(agent.model)  # its own client: the turn may be using agent.llm
    completion = llm.complete(side_messages(agent.session.messages, question),
                              agent._tool_schema(), stream=False)
    with _USAGE_LOCK:
        agent.session.record_usage(completion.usage, agent.model)
    answer = (completion.content or "").strip()
    if not answer:
        return "(no answer: the model replied with a tool call instead; ask again " \
               "after the turn, or rephrase)"
    return answer
