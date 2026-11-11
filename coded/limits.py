"""Per-model limits that scale with the model's context window.

The base values are tuned for a 128k-token window (and equal coded's historical
constants). Other windows scale them by ``context_window / 128_000`` (clamped to
1/8×–8×, with floors so small models stay usable). Per-model overrides::

    "models": {"big": {"context_window": 300000,
                       "limits": {"read_lines": 10000, "shell_output_chars": 200000}}}

Two caps keep bigger limits from overflowing the window: every tool result is cut
to ``tool_result_chars`` (~9% of the window) and all results of one model step
together to ``step_result_chars`` (~25% of the window). Characters ≈ tokens × 4.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import Any, Dict, List

BASE_CONTEXT = 128_000
_CHARS_PER_TOKEN = 4
_MIN_FACTOR, _MAX_FACTOR = 0.125, 8.0


@dataclass
class Limits:
    read_lines: int = 2000              # read tool, lines per call
    shell_output_chars: int = 30_000    # bash output kept
    git_output_chars: int = 30_000      # git tool output kept
    web_fetch_chars: int = 12_000       # web_fetch page text kept
    grep_matches: int = 200             # grep/glob results
    external_output_chars: int = 40_000  # outside agent output kept
    instructions_file_chars: int = 12_000   # per AGENTS.md/CODED.md file
    instructions_total_chars: int = 32_000  # all instruction files together
    nested_instructions_chars: int = 12_000  # subdirectory instructions, per load
    compact_keep_recent: int = 6        # messages kept verbatim when compacting
    tool_result_chars: int = 46_080     # hard cap on any single tool result
    step_result_chars: int = 128_000    # hard cap on all results of one step
    repo_overview_chars: int = 0        # repository overview in the prompt (0 = off)


# Floors for scaled-down (small-context) models.
_FLOORS = {
    "read_lines": 200, "shell_output_chars": 4000, "git_output_chars": 4000,
    "web_fetch_chars": 3000, "grep_matches": 50, "external_output_chars": 4000,
    "instructions_file_chars": 4000, "instructions_total_chars": 8000,
    "nested_instructions_chars": 3000, "compact_keep_recent": 4,
    "tool_result_chars": 4000, "step_result_chars": 8000,
}
_SCALED = tuple(k for k in _FLOORS if k not in ("tool_result_chars", "step_result_chars"))
LIMIT_KEYS = tuple(f.name for f in fields(Limits))


def scale_factor(context_window: int) -> float:
    return max(_MIN_FACTOR, min(_MAX_FACTOR, (context_window or BASE_CONTEXT) / BASE_CONTEXT))


def repo_overview_enabled(model: Any) -> bool:
    setting = getattr(model, "repo_overview", "auto")
    if isinstance(setting, bool):
        return setting
    if str(setting).lower() in ("on", "true", "yes"):
        return True
    if str(setting).lower() in ("off", "false", "no"):
        return False
    return (getattr(model, "context_window", BASE_CONTEXT) or BASE_CONTEXT) >= 200_000


def limits_for(model: Any) -> Limits:
    """Limits for a ModelConfig (or anything with context_window / limits)."""
    window = getattr(model, "context_window", BASE_CONTEXT) or BASE_CONTEXT
    f = scale_factor(window)
    base = Limits()
    values: Dict[str, int] = asdict(base)
    if f != 1.0:
        for key in _SCALED:
            values[key] = max(_FLOORS[key], int(round(getattr(base, key) * f)))
    window_chars = window * _CHARS_PER_TOKEN
    values["tool_result_chars"] = max(_FLOORS["tool_result_chars"], int(window_chars * 0.09))
    values["step_result_chars"] = max(_FLOORS["step_result_chars"], int(window_chars * 0.25))
    if repo_overview_enabled(model):
        values["repo_overview_chars"] = min(60_000, int(window_chars * 0.03))
    for key, val in (getattr(model, "limits", None) or {}).items():
        if key in values:
            try:
                values[key] = max(0, int(val))
            except (TypeError, ValueError):
                pass
    return Limits(**values)


def unknown_limit_keys(model: Any) -> List[str]:
    return sorted(k for k in (getattr(model, "limits", None) or {}) if k not in LIMIT_KEYS)


def get_limits(ctx: Any) -> Limits:
    """The calling agent's limits from a ToolContext (defaults if absent)."""
    extra = getattr(ctx, "extra", None) or {}
    lim = extra.get("limits")
    return lim if isinstance(lim, Limits) else Limits()


def cap_text(text: str, max_chars: int, hint: str = "") -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    note = f"\n… (truncated: {len(text) - max_chars} more chars"
    note += f"; {hint})" if hint else ")"
    return text[:max_chars] + note
