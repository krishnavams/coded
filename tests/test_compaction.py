"""Tests for context auto-compaction."""

from __future__ import annotations

from coded.compaction import compaction_threshold, estimate_tokens, maybe_compact
from coded.config import ModelConfig
from coded.llm import Completion, Usage
from coded.session import Session


class _FakeLLM:
    """Stand-in LLM whose complete() returns a fixed summary."""
    def __init__(self):
        self.calls = 0

    def complete(self, messages, tools=None, stream=False):
        self.calls += 1
        return Completion(content="- did stuff\n- edited a.py", tool_calls=[], usage=Usage())


def _session_with_history(n_pairs):
    s = Session(system_prompt="SYS")
    for i in range(n_pairs):
        s.add_user(f"question {i} " + "x" * 50)
        s.add_assistant(f"answer {i} " + "y" * 50)
    return s


def test_estimate_tokens_counts_content():
    msgs = [{"role": "user", "content": "a" * 40}]
    assert estimate_tokens(msgs) == 10  # 40 chars // 4


def test_no_compaction_when_small():
    model = ModelConfig(name="m", model="m", context_window=100_000)
    s = _session_with_history(3)
    llm = _FakeLLM()
    assert maybe_compact(s, model, llm) is False
    assert llm.calls == 0


def test_compaction_triggers_and_preserves_tail():
    # Tiny context window so the threshold is easily exceeded.
    model = ModelConfig(name="m", model="m", context_window=200, max_tokens=0)
    s = _session_with_history(10)
    before = len(s.messages)
    llm = _FakeLLM()

    did = maybe_compact(s, model, llm, keep_recent=4)
    assert did is True
    assert llm.calls == 1
    # System prompt kept, a summary inserted, and the history is now shorter.
    assert s.messages[0]["content"] == "SYS"
    assert "summarized" in s.messages[1]["content"].lower()
    assert len(s.messages) < before
    # The tail begins at a clean 'user' boundary (no orphaned tool messages).
    assert s.messages[2]["role"] == "user"
    assert s.compactions == 1


def test_force_compaction_ignores_threshold():
    model = ModelConfig(name="m", model="m", context_window=1_000_000)
    s = _session_with_history(8)
    llm = _FakeLLM()
    assert maybe_compact(s, model, llm, force=True, keep_recent=4) is True


def test_threshold_reserves_output_room():
    model = ModelConfig(name="m", model="m", context_window=1000, max_tokens=200)
    assert compaction_threshold(model, 0.8) == 600  # 800 - 200
