"""Tool framework: base class, result type, execution context, and registry."""

from __future__ import annotations

import copy
import fnmatch
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple


@dataclass
class ToolResult:
    content: str
    is_error: bool = False

    @classmethod
    def ok(cls, content: str) -> "ToolResult":
        return cls(content, False)

    @classmethod
    def error(cls, content: str) -> "ToolResult":
        return cls(content, True)


@dataclass
class ToolContext:
    """Everything a tool needs to run, injected by the agent."""

    cwd: str
    permissions: Any  # PermissionManager
    config: Any  # Config
    # Runs a sub-agent with `prompt` and returns its final text. Injected by Agent.
    spawn_subagent: Optional[Callable[[str], str]] = None
    # Records pre-change file state for /undo. Injected by Agent. May be None.
    checkpoints: Any = None  # CheckpointManager
    # Shared agent state: "todos" (TodoStore), "shells" (BackgroundShells),
    # "set_mode"/"get_mode" (plan mode), "external_agents", ...
    extra: Dict[str, Any] = field(default_factory=dict)


class Tool:
    """Base class for all tools."""

    name: str = ""
    description: str = ""
    # A shorter description for the compact prompt profile (small/local
    # models), where every token of every tool definition is paid each step.
    # Keep the rules that prevent mistakes; drop the explanation.
    brief: str = ""
    # JSON Schema for the tool's arguments.
    parameters: Dict[str, Any] = {"type": "object", "properties": {}}
    requires_permission: bool = False
    # True for a tool that changes things outside the conversation but gates
    # itself per call (git). It makes the turn eligible for verification; a
    # tool with requires_permission already is.
    mutates: bool = False
    # Read-only tools that may run concurrently with each other.
    parallel_safe: bool = False
    # Output comes from outside the user's control (web pages, PR bodies, MCP).
    untrusted_output: bool = False

    def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        raise NotImplementedError

    # -- helpers ------------------------------------------------------------
    def permission_detail(self, args: Dict[str, Any]) -> str:
        """Human-readable description of what this call will do (for prompts)."""
        return f"{self.name}({args})"

    def permission_target(self, args: Dict[str, Any]) -> str:
        """The string matched against allow/deny rules (e.g. command or path)."""
        return ""

    def is_parallel_safe(self, args: Dict[str, Any], ctx: "ToolContext") -> bool:
        return self.parallel_safe

    def auto_allowed(self, args: Dict[str, Any], ctx: "ToolContext") -> bool:
        """True when this call may skip the prompt (e.g. it runs sandboxed)."""
        return False

    def escalation(self, args: Dict[str, Any], ctx: "ToolContext") -> Optional[Dict[str, str]]:
        """A call that steps outside a safety boundary (e.g. bash without the
        sandbox) returns {"key", "title", "detail"}: the agent then asks that
        question on its own — `always_ask`, never answered by the ordinary
        approval of this tool, a hook or the sandbox's auto-allow."""
        return None

    def preview(self, args: Dict[str, Any], ctx: "ToolContext"):
        """Optional colored/plain preview (e.g. a diff) shown before running."""
        return None

    def to_openai(self, compact: bool = False) -> Dict[str, Any]:
        description = (self.brief or self.description) if compact else self.description
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": description.strip(),
                "parameters": _compact_parameters(self.parameters) if compact else self.parameters,
            },
        }


_PARAM_DESC_MAX = 120


def _compact_parameters(schema: Any) -> Any:
    """Long argument descriptions cut to their first sentence (compact profile)."""
    if not isinstance(schema, dict):
        return schema
    out = copy.deepcopy(schema)

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            desc = node.get("description")
            if isinstance(desc, str) and len(desc) > _PARAM_DESC_MAX:
                m = re.search(r"(?<=[.!?])\s", desc)
                cut = desc[: m.start()] if m and m.start() <= _PARAM_DESC_MAX else desc
                if len(cut) > _PARAM_DESC_MAX:
                    cut = cut[: _PARAM_DESC_MAX - 1].rsplit(" ", 1)[0] + "…"
                node["description"] = cut
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(out)
    return out


def tool_disabled(name: str, patterns: Iterable[str]) -> bool:
    """Does `name` match any disabled-tool pattern ("github", "mcp__slack__*")?"""
    return any(fnmatch.fnmatchcase(name, p) for p in patterns or ())


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: Dict[str, Tool] = {}

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def all(self) -> List[Tool]:
        return list(self._tools.values())

    def openai_schema(self, compact: bool = False,
                      exclude: Iterable[str] = ()) -> List[Dict[str, Any]]:
        exclude = list(exclude or ())
        return [t.to_openai(compact) for t in self._tools.values()
                if not tool_disabled(t.name, exclude)]

    def names(self) -> List[str]:
        return list(self._tools)


# ---------------------------------------------------------------------------
# Argument validation against a tool's JSON schema (before it runs)
# ---------------------------------------------------------------------------

MAX_ARGUMENT_BYTES = 8 * 1024 * 1024  # one tool call's JSON arguments
_TYPES = {"string": str, "boolean": bool, "object": dict, "array": list}


def _coerce(value: Any, kind: str) -> Any:
    """Forgive what small models commonly send: "5" for 5, "true" for true.
    Returns the coerced value, or the original when it cannot be."""
    if kind == "integer":
        if isinstance(value, bool):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            return int(value.strip())
    if kind == "number" and isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return value
    if kind == "boolean" and isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"
    return value


def _type_ok(value: Any, kind: str) -> bool:
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == "null":
        return value is None
    expected = _TYPES.get(kind)
    return True if expected is None else isinstance(value, expected)


def _check(value: Any, schema: Dict[str, Any], where: str, depth: int = 0) -> Tuple[Any, Optional[str]]:
    """(possibly coerced value, error or None). Only the keywords coded's and
    common MCP schemas use are checked; anything else is accepted. Only the
    top level and its fields: several tools deliberately accept looser nested
    shapes (ask_user takes plain-string options) and check those themselves."""
    if not isinstance(schema, dict) or depth > 1:
        return value, None
    kinds = schema.get("type")
    if kinds:
        kinds = kinds if isinstance(kinds, list) else [kinds]
        if not any(_type_ok(value, k) for k in kinds):
            for k in kinds:
                coerced = _coerce(value, k)
                if _type_ok(coerced, k):
                    value = coerced
                    break
            else:
                got = type(value).__name__ if value is not None else "null"
                return value, f"{where} must be {' or '.join(kinds)} (got {got})"
    if "enum" in schema and isinstance(schema["enum"], list) and value not in schema["enum"]:
        return value, f"{where} must be one of {', '.join(map(str, schema['enum']))}"
    if isinstance(value, dict) and depth == 0:
        props = schema.get("properties") or {}
        for name in schema.get("required") or []:
            if value.get(name) is None:
                return value, f"{where}.{name} is required" if where != "arguments" \
                    else f"'{name}' is required"
        if schema.get("additionalProperties") is False:
            extra = sorted(set(value) - set(props))
            if extra:
                return value, f"unexpected argument(s) in {where}: {', '.join(extra)}"
        out = dict(value)
        for name, sub in props.items():
            if name in value and value[name] is not None:
                label = name if where == "arguments" else f"{where}.{name}"
                out[name], err = _check(value[name], sub, label, depth + 1)
                if err:
                    return value, err
        return out, None
    return value, None


def validate_arguments(schema: Dict[str, Any], args: Dict[str, Any]
                       ) -> Tuple[Dict[str, Any], Optional[str]]:
    """Check a call's arguments against the tool's schema: required fields,
    types (with the common string-number/boolean forgiveness), enums and
    unknown fields where the schema forbids them (additionalProperties: false). Returns the
    (coerced) arguments and an error message for the model, or None."""
    value, err = _check(args, schema or {}, "arguments")
    return (value if isinstance(value, dict) else args), err
