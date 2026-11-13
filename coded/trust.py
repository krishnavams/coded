"""Project trust: what a repository's own `.coded/` may do before you say so.

A cloned repository's `./.coded/config.json` (and its project-scope plugins)
is written by whoever wrote the repository. Settings that only shape output
apply at once, but anything that runs a command, reaches a network endpoint
or loosens the permission gate — hooks, MCP servers, outside agents, model
endpoints, auto-approval, allow rules, sandbox, … — applies only once the
project is trusted. Trust is remembered per folder together with a
fingerprint of exactly those settings, so a `git pull` that changes a hook or
an endpoint asks again.

Interactive front-ends ask once at start (`cli._ask_project_trust`); a
non-interactive run (`-p`, `coded review`, ACP, MCP server) treats an unknown
project as untrusted unless `--trust-project` or `CODED_TRUST_PROJECT=1`.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

# Project config keys that apply without trust: they only change how coded
# looks or which of *your* models it picks, never what it runs or where it
# connects. Everything else is sensitive.
SAFE_KEYS = frozenset({
    "default_model", "model_roles", "pools", "routing", "max_turns", "auto_extend_turns", "stream",
    "auto_compact", "compact_ratio", "auto_fallback", "show_reasoning",
    "parallel_tools", "disabled_tools", "editing_mode", "max_cost", "notify",
    "notify_after", "output_style", "update_check", "embedding_model",
})
_GUARD_ORDER = {"off": 0, "warn": 1, "strict": 2}


def store_path() -> Path:
    env = os.environ.get("CODED_TRUST_STORE")
    if env:
        return Path(env).expanduser()
    from coded.config import user_config_path

    return user_config_path().parent / "trusted.json"


def env_trusts() -> bool:
    return os.environ.get("CODED_TRUST_PROJECT", "").strip().lower() in ("1", "true", "yes")


def split(data: Dict[str, Any], current_guard: str = "warn") -> tuple:
    """(safe, sensitive) parts of a project config. Deny rules and a stricter
    injection guard only tighten things, so they are safe."""
    safe: Dict[str, Any] = {}
    sensitive: Dict[str, Any] = {}
    for key, value in data.items():
        if key in SAFE_KEYS:
            safe[key] = value
        elif key == "permissions" and isinstance(value, dict):
            if value.get("deny"):
                safe["permissions"] = {"deny": list(value["deny"])}
            rest = {k: v for k, v in value.items() if k != "deny" and v}
            if rest:
                sensitive["permissions"] = rest
        elif key == "injection_guard":
            if _GUARD_ORDER.get(str(value), -1) >= _GUARD_ORDER.get(current_guard, 1):
                safe[key] = value
            else:
                sensitive[key] = value
        elif key == "models" and isinstance(value, dict):
            for name, spec in value.items():
                part = sensitive if _model_is_sensitive(spec) else safe
                part.setdefault("models", {})[name] = spec
        elif key in ("auto_approve", "accept_edits") and not value:
            safe[key] = value
        else:
            sensitive[key] = value
    return safe, sensitive


# A model that sets any of these sends requests (and maybe your key) somewhere
# other than its provider's public endpoint, or changes how they are checked.
_ENDPOINT_FIELDS = ("base_url", "api_key", "api_key_env", "extra_headers", "ca_bundle")


def _model_is_sensitive(spec: Any) -> bool:
    if not isinstance(spec, dict):
        return True
    from coded.config import PROVIDERS

    if any(spec.get(f) for f in _ENDPOINT_FIELDS):
        return True
    if spec.get("verify_ssl") is False:
        return True
    return str(spec.get("provider", "openai")) not in PROVIDERS


def fingerprint(sensitive: Dict[str, Any]) -> str:
    blob = json.dumps(sensitive, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _key(root: str) -> str:
    return os.path.realpath(root)


def _load() -> Dict[str, Any]:
    try:
        data = json.loads(store_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def is_trusted(root: str, fp: str) -> bool:
    return _load().get(_key(root), {}).get("fingerprint") == fp


def remember(root: str, fp: str) -> None:
    path = store_path()
    data = _load()
    data.setdefault(_key(root), {})["fingerprint"] = fp
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def forget(root: str) -> bool:
    data = _load()
    if data.pop(_key(root), None) is None:
        return False
    path = store_path()
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    return True


def _commands(spec: Any) -> List[str]:
    """Every command found in a hook / server / agent spec, verbatim (with its
    args), however deeply the spec nests them."""
    out: List[str] = []
    if isinstance(spec, dict):
        cmd = spec.get("command")
        if isinstance(cmd, str) and cmd:
            args = spec.get("args")
            extra = [str(a) for a in args] if isinstance(args, list) else []
            out.append(" ".join([cmd] + extra))
        elif isinstance(cmd, list):
            out.append(" ".join(str(c) for c in cmd))
        for key, value in spec.items():
            if key not in ("command", "args") and isinstance(value, (dict, list)):
                out += _commands(value)
    elif isinstance(spec, list):
        for value in spec:
            out += _commands(value)
    return out


def describe(sensitive: Dict[str, Any]) -> List[str]:
    """Human lines for the trust prompt: each setting, with every command and
    endpoint written out verbatim."""
    lines: List[str] = []
    for key in sorted(sensitive):
        value = sensitive[key]
        if key == "hooks" and isinstance(value, dict):
            for event, entries in value.items():
                for cmd in dict.fromkeys(_commands(entries)):
                    lines.append(f"hook {event}: runs `{cmd}`")
        elif key in ("mcp_servers", "external_agents") and isinstance(value, dict):
            what = "MCP server" if key == "mcp_servers" else "outside agent"
            for name, spec in value.items():
                cmds = _commands(spec) or [json.dumps(spec)[:120]]
                lines.append(f"{what} {name}: runs `{cmds[0]}`")
        elif key == "models" and isinstance(value, dict):
            for name, spec in value.items():
                spec = spec if isinstance(spec, dict) else {}
                where = spec.get("base_url") or f"provider {spec.get('provider', '?')}"
                lines.append(f"model {name}: sends requests to {where}")
        elif key == "plugins" and isinstance(value, dict):
            for name, contrib in value.items():
                lines.append(f"project plugin {name}:")
                lines += ["  " + ln for ln in describe(contrib)]
        elif key == "permissions":
            lines.append(f"permissions: {json.dumps(value)}")
        elif key in ("auto_approve", "accept_edits"):
            lines.append(f"{key}: {value} (actions would run without asking)")
        else:
            text = json.dumps(value) if not isinstance(value, str) else value
            lines.append(f"{key}: {text[:160]}")
    return lines


def project_plugin_contributions(cwd: Optional[str]) -> Dict[str, Any]:
    """What project-scope plugins would add (hooks, MCP servers, agents)."""
    try:
        from coded.plugins import installed_plugins
    except Exception:  # noqa: BLE001
        return {}
    out: Dict[str, Any] = {}
    try:
        plugins = installed_plugins(cwd)
    except Exception:  # noqa: BLE001 - a broken registry is reported elsewhere
        return {}
    for p in plugins:
        if getattr(p, "scope", "user") != "project":
            continue
        try:
            contrib = {"hooks": p.hooks(), "mcp_servers": p.mcp_servers(),
                       "external_agents": p.external_agents()}
        except Exception:  # noqa: BLE001
            contrib = {}
        out[p.name] = {k: v for k, v in contrib.items() if v} or {"files": str(p.root)}
    return out


# ---------------------------------------------------------------------------
# Self-healing: the detected check command runs the repository's own code
# ---------------------------------------------------------------------------
def _check_fp(command: str) -> str:
    return hashlib.sha256(command.encode("utf-8")).hexdigest()


def check_approved(root: str, command: str) -> bool:
    return _check_fp(command) in (_load().get(_key(root), {}).get("checks") or [])


def approve_check(root: str, command: str) -> None:
    data = _load()
    entry = data.setdefault(_key(root), {})
    checks = list(entry.get("checks") or [])
    if _check_fp(command) not in checks:
        checks.append(_check_fp(command))
    entry["checks"] = checks
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)
