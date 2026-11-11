"""Interactive model configuration: `coded config setup` and the REPL's /setup.

The wizard edits **one config file's raw JSON** — never a merged `Config`. A
loaded `Config` carries the other layer's models and plugin contributions, and
writing those back would bake them into the user's file. So the file's parsed
dict is mutated in place, which also preserves keys the wizard knows nothing
about (per-model `limits`, `prompts_dir`, `fallback`, hooks, MCP servers …).

Everything goes through the `Prompter` callables, so tests drive the whole flow
with scripted answers and no TTY.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from coded.config import (
    LOCAL_PROVIDERS,
    PROVIDERS,
    Config,
    ConfigError,
    ModelConfig,
    project_config_path,
    user_config_path,
)

ABORT = "__abort__"


class Abort(Exception):
    """The user pressed Ctrl-C / Ctrl-D: nothing is written."""


@dataclass
class Prompter:
    """How the wizard talks to the user (swapped out in tests)."""

    ask: Callable[[str, str], str]  # (question, default) -> answer
    secret: Callable[[str], str]  # (question) -> answer, not echoed
    out: Callable[[str], None]

    def text(self, question: str, default: str = "") -> str:
        suffix = f" [{default}]" if default else ""
        answer = self.ask(f"{question}{suffix}: ", default).strip()
        return answer or default

    def yes(self, question: str, default: bool = False) -> bool:
        answer = self.text(f"{question} ({'Y/n' if default else 'y/N'})").strip().lower()
        if not answer or answer in ("y/n", "yes/no"):
            return default
        return answer in ("y", "yes")

    def choose(self, question: str, options: Sequence[str],
               default: Optional[str] = None) -> Optional[str]:
        """Pick one option by number or by name. None = nothing chosen."""
        if not options:
            return None
        for i, opt in enumerate(options, 1):
            self.out(f"  {i:>2}. {opt}")
        answer = self.text(question, default or "")
        if not answer:
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return options[int(answer) - 1]
        return answer if answer in options else None

    def choose_many(self, question: str, options: Sequence[str]) -> List[str]:
        """Pick several by number ("1,3-5"), by name, or "*" for all."""
        for i, opt in enumerate(options, 1):
            self.out(f"  {i:>2}. {opt}")
        answer = self.text(question)
        if not answer:
            return []
        if answer.strip() == "*":
            return list(options)
        picked: List[str] = []
        for part in answer.replace(" ", ",").split(","):
            if not part:
                continue
            if "-" in part and all(p.isdigit() for p in part.split("-", 1)):
                lo, hi = (int(p) for p in part.split("-", 1))
                picked += [options[i - 1] for i in range(lo, hi + 1) if 1 <= i <= len(options)]
            elif part.isdigit() and 1 <= int(part) <= len(options):
                picked.append(options[int(part) - 1])
            elif part in options:
                picked.append(part)
        return list(dict.fromkeys(picked))


def console_prompter() -> Prompter:
    import getpass

    from coded import ui

    def ask(question: str, _default: str) -> str:
        try:
            return input(question)
        except (EOFError, KeyboardInterrupt):
            raise Abort from None

    def secret(question: str) -> str:
        try:
            return getpass.getpass(question)
        except (EOFError, KeyboardInterrupt):
            raise Abort from None

    return Prompter(ask=ask, secret=secret, out=ui.console.print)


# ---------------------------------------------------------------------------
# The file being edited
# ---------------------------------------------------------------------------
def target_path(*, project: bool = False, config_path: Optional[str] = None,
                cwd: Optional[str] = None) -> Path:
    if config_path:
        return Path(config_path).expanduser()
    return project_config_path(cwd) if project else user_config_path()


def read_file(path: Path) -> Dict[str, Any]:
    """The file's own JSON (not a merged Config). Invalid JSON is an error: the
    wizard must never silently replace a file it could not read."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} is not valid JSON ({exc}). Fix or move it first.") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path} does not contain a JSON object.")
    return data


def holds_a_key(data: Dict[str, Any]) -> bool:
    """Does this config embed a literal API key (rather than an env var name)?"""
    return any(entry.get("api_key") for entry in (data.get("models") or {}).values()
               if isinstance(entry, dict))


def write_file(path: Path, data: Dict[str, Any], *, secret: Optional[bool] = None) -> None:
    """Write atomically, so an interrupted save cannot truncate the config.

    The mode is decided from the data, not from what this session happened to
    type: a file that already held a key must not become world-readable because
    the edit was only a rename. An existing file keeps the mode it has.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    secret = holds_a_key(data) if secret is None else (secret or holds_a_key(data))
    mode = None
    try:
        mode = path.stat().st_mode & 0o777
    except OSError:
        pass
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(tmp, mode if mode is not None else (0o600 if secret else 0o644))
    except OSError:  # a no-op on Windows
        pass
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Model entries
# ---------------------------------------------------------------------------
def _models(data: Dict[str, Any]) -> Dict[str, Any]:
    models = data.setdefault("models", {})
    if not isinstance(models, dict):
        raise ConfigError('The "models" key is not an object.')
    return models


def list_endpoint_models(entry: Dict[str, Any], alias: str = "probe") -> List[str]:
    """Model ids the endpoint advertises. Many gateways do not serve /models."""
    from coded.llm import build_openai_client

    mc = ModelConfig.from_dict(alias, {**entry, "model": entry.get("model") or "probe"})
    client = build_openai_client(mc)
    return sorted(m.id for m in client.models.list().data if getattr(m, "id", None))


def probe_entry(entry: Dict[str, Any], alias: str) -> List[tuple]:
    from coded.llm import probe_stages

    return probe_stages(ModelConfig.from_dict(alias, entry))


def dangling_references(data: Dict[str, Any]) -> List[str]:
    """References to aliases this file does not define.

    Only a warning: another layer may define them. Removing a model is the
    common way to create one.
    """
    known = set(_models(data)) | set((data.get("pools") or {}))
    out = []
    for key in ("default_model",):
        value = data.get(key)
        if value and value not in known:
            out.append(f'{key} = "{value}"')
    for role, alias in (data.get("model_roles") or {}).items():
        if alias not in known:
            out.append(f'model_roles.{role} = "{alias}"')
    for level, alias in (data.get("routing") or {}).items():
        if level in ("simple", "medium", "hard") and alias and alias not in known:
            out.append(f'routing.{level} = "{alias}"')
    for pool, members in (data.get("pools") or {}).items():
        for member in members or []:
            if member not in known:
                out.append(f'pools.{pool} → "{member}"')
    for name, entry in _models(data).items():
        for fb in (entry.get("fallback") or []):
            if fb not in known:
                out.append(f'models.{name}.fallback → "{fb}"')
    return out


# ---------------------------------------------------------------------------
# The wizard
# ---------------------------------------------------------------------------
class Wizard:
    def __init__(self, path: Path, prompter: Prompter, *, allow_literal_key: bool = True):
        self.path = path
        self.p = prompter
        self.data = read_file(path)
        self.original = json.dumps(self.data, sort_keys=True)
        self.allow_literal_key = allow_literal_key
        self.has_secret = False

    # -- helpers ------------------------------------------------------------
    @property
    def models(self) -> Dict[str, Any]:
        return _models(self.data)

    def changed(self) -> bool:
        return json.dumps(self.data, sort_keys=True) != self.original

    def _provider_entry(self) -> Dict[str, Any]:
        """Ask for provider / base_url / key source. Shared by add and edit."""
        names = sorted(PROVIDERS)
        self.p.out("\nWhere does this model live?")
        provider = self.p.choose("Provider (number or name)", names, "openai-compatible") \
            or "openai-compatible"
        entry: Dict[str, Any] = {"provider": provider}
        preset = PROVIDERS.get(provider, {})
        default_url = preset.get("base_url", "")
        if provider == "openai-compatible" or not default_url:
            url = self.p.text("Base URL (e.g. https://gateway.example.com/v1)")
            if not url:
                raise ConfigError("A custom endpoint needs a base_url.")
            entry["base_url"] = url
        else:
            url = self.p.text("Base URL", default_url)
            if url != default_url:
                entry["base_url"] = url

        env_name = preset.get("api_key_env") or "OPENAI_API_KEY"
        if provider in LOCAL_PROVIDERS:
            self.p.out("  Local server — no API key needed.")
            return entry
        self.p.out(f"\nAPI key. An environment variable is safer: the config file holds "
                   f"only its name.\n  Currently {env_name} is "
                   f"{'set' if os.environ.get(env_name) else 'not set'} in this shell.")
        how = self.p.text("Read the key from an env var, or store it in this file? "
                          "(env/store)", "env").lower()
        if how.startswith("s") and self.allow_literal_key:
            key = self.p.secret("API key (not echoed): ").strip()
            if key:
                entry["api_key"] = key
                self.has_secret = True
            return entry
        if how.startswith("s"):
            self.p.out("  This file is shared with the project, so keys stay in env vars.")
        name = self.p.text("Environment variable name", env_name)
        entry.pop("api_key", None)  # never leave a stale literal key behind
        if name != preset.get("api_key_env"):
            entry["api_key_env"] = name
        return entry

    def _ask_model_fields(self, entry: Dict[str, Any], model_id: str) -> Dict[str, Any]:
        entry = {**entry, "model": model_id}
        window = self.p.text("Context window in tokens", str(entry.get("context_window", 128000)))
        try:
            entry["context_window"] = int(float(window))
        except ValueError:
            entry["context_window"] = 128_000
        if not self.p.yes("Does it support tools (function calling)?", True):
            entry["supports_tools"] = False
        if self.p.yes("Does it accept images?", bool(entry.get("supports_vision"))):
            entry["supports_vision"] = True
        return entry

    # -- actions ------------------------------------------------------------
    def add_models(self) -> None:
        entry = self._provider_entry()
        ids: List[str] = []
        if self.p.yes("\nAsk the endpoint which models it serves?", True):
            try:
                ids = list_endpoint_models(entry)
            except Exception as exc:  # noqa: BLE001 - plenty of gateways refuse this
                self.p.out(f"  Could not list models ({type(exc).__name__}); "
                           "type the model id instead.")
        chosen: List[str] = []
        if ids:
            self.p.out(f"\n{len(ids)} model(s) available:")
            chosen = self.p.choose_many("Add which? (numbers, ranges, * for all)", ids)
        if not chosen:
            model_id = self.p.text("Model id (as the API spells it)")
            if not model_id:
                return
            chosen = [model_id]
        for model_id in chosen:
            alias = self.p.text(f"Alias for '{model_id}'", _default_alias(model_id, self.models))
            if not alias:
                continue
            if alias in self.models and not self.p.yes(f"'{alias}' exists — replace it?", False):
                continue
            self.models[alias] = self._ask_model_fields(entry, model_id)
            self.p.out(f"  Added '{alias}'.")
            if self.p.yes(f"Send a test request to '{alias}' now?", True):
                self.test(alias)
        if self.models and not self.data.get("default_model"):
            self.data["default_model"] = next(iter(self.models))
            self.p.out(f"  Default model: {self.data['default_model']}")

    def edit(self, alias: str) -> None:
        entry = dict(self.models[alias])
        self.p.out(f"\nEditing '{alias}': {json.dumps(entry, indent=2)}")
        field = self.p.choose("Change which setting?",
                              ["model", "base_url", "api key", "context_window",
                               "supports_tools", "supports_vision", "temperature",
                               "max_tokens", "timeout", "fallback", "rename"])
        if field is None:
            return
        if field == "api key":
            fresh = self._provider_entry()
            for stale in ("api_key", "api_key_env", "base_url"):
                entry.pop(stale, None)
            self.models[alias] = {**entry, **fresh, "model": entry.get("model")}
            return
        if field == "rename":
            new = self.p.text("New alias", alias)
            if new and new != alias:
                self.models[new] = self.models.pop(alias)
                self._rename_references(alias, new)
            return
        if field == "fallback":
            others = [a for a in self.models if a != alias]
            entry["fallback"] = self.p.choose_many(
                "Fall back to which models, in order?", others)
            if not entry["fallback"]:
                entry.pop("fallback")
        elif field in ("supports_tools", "supports_vision"):
            entry[field] = self.p.yes(f"{field}?", bool(entry.get(field, field == "supports_tools")))
        else:
            value = self.p.text(field, str(entry.get(field, "")))
            if value == "":
                entry.pop(field, None)
            else:
                entry[field] = _coerce(field, value)
        self.models[alias] = entry

    def remove(self, alias: str) -> None:
        if not self.p.yes(f"Remove '{alias}'?", False):
            return
        self.models.pop(alias, None)
        if self.data.get("default_model") == alias:
            self.data.pop("default_model")
            remaining = list(self.models)
            if remaining:
                self.data["default_model"] = self.p.choose("New default model", remaining,
                                                           remaining[0]) or remaining[0]

    def _rename_references(self, old: str, new: str) -> None:
        if self.data.get("default_model") == old:
            self.data["default_model"] = new
        for key in ("model_roles", "routing"):
            section = self.data.get(key) or {}
            for k, v in list(section.items()):
                if v == old:
                    section[k] = new
        for pool, members in (self.data.get("pools") or {}).items():
            self.data["pools"][pool] = [new if m == old else m for m in members or []]
        for entry in self.models.values():
            if entry.get("fallback"):
                entry["fallback"] = [new if m == old else m for m in entry["fallback"]]

    def test(self, alias: str) -> None:
        entry = self.models.get(alias)
        if entry is None:
            return
        self.p.out(f"  Probing '{alias}' …")
        try:
            stages = probe_entry(entry, alias)
        except Exception as exc:  # noqa: BLE001
            self.p.out(f"  ✗ could not reach it: {type(exc).__name__}: {exc}")
            return
        from coded.llm import probe_hint

        for label, exc in stages:
            if exc is None:
                self.p.out(f"    ✓ {label}")
                continue
            detail = str(exc).splitlines()[0][:200] if str(exc) else type(exc).__name__
            self.p.out(f"    ✗ {label}: {type(exc).__name__}: {detail}")
            self.p.out(f"    → {probe_hint(label, exc)}")
            if label == "tools" and self.p.yes(
                    "Set supports_tools: false for this model and probe again?", True):
                entry["supports_tools"] = False
                self.test(alias)
            return
        self.p.out("  ✓ every request shape was accepted.")

    def set_roles(self) -> None:
        targets = list(self.models) + list(self.data.get("pools") or {})
        if not targets:
            return
        roles = self.data.setdefault("model_roles", {})
        self.p.out("\nA role sends one kind of work to a different model.")
        for role, what in (("plan", "planning in plan mode"),
                           ("subagent", "sub-agents spawned by the task tool"),
                           ("compact", "summarizing old context"),
                           ("review", "the review pipeline"),
                           ("verdict", "the review verdict")):
            current = roles.get(role, "")
            pick = self.p.choose(f"{role} — {what}", targets + ["(none)"], current)
            if pick in (None, "(none)"):
                roles.pop(role, None)
            else:
                roles[role] = pick
        if not roles:
            self.data.pop("model_roles", None)

    def set_pool(self) -> None:
        pools = self.data.setdefault("pools", {})
        name = self.p.text("Pool name (interchangeable models behind one name)")
        if not name:
            self.data.pop("pools", None) if not pools else None
            return
        if name in self.models:
            self.p.out("  That name is already a model alias; pick another.")
            return
        members = self.p.choose_many(f"Members of '{name}'", list(self.models))
        if members:
            pools[name] = members
        else:
            pools.pop(name, None)
        if not pools:
            self.data.pop("pools", None)

    def set_routing(self) -> None:
        targets = list(self.models) + list(self.data.get("pools") or {})
        routing = self.data.setdefault("routing", {})
        self.p.out("\nRouting sends a request to a model chosen by how hard it looks.")
        routing["enabled"] = self.p.yes("Enable difficulty routing?",
                                        bool(routing.get("enabled")))
        if not routing["enabled"]:
            if len(routing) == 1:
                self.data.pop("routing")
            return
        for level, what in (("simple", "typos, renames, questions"),
                            ("medium", "ordinary implementation work"),
                            ("hard", "architecture, debugging, security")):
            pick = self.p.choose(f"{level} — {what}", targets + ["(none)"],
                                 routing.get(level, ""))
            if pick in (None, "(none)"):
                routing.pop(level, None)
            else:
                routing[level] = pick

    # -- the loop -----------------------------------------------------------
    def summary(self) -> str:
        lines = [f"Config file: {self.path}"]
        for alias, entry in self.models.items():
            marker = "*" if alias == self.data.get("default_model") else " "
            bits = [entry.get("provider", "openai"), entry.get("model", "?")]
            if entry.get("base_url"):
                bits.append(entry["base_url"])
            if entry.get("api_key"):
                bits.append("key: stored in this file")
            elif entry.get("api_key_env"):
                bits.append(f"key: ${entry['api_key_env']}")
            if entry.get("fallback"):
                bits.append("fallback→" + ",".join(entry["fallback"]))
            lines.append(f" {marker} {alias:<16} " + "  ".join(str(b) for b in bits))
        for pool, members in (self.data.get("pools") or {}).items():
            lines.append(f"   {pool:<16} pool: {', '.join(members)}")
        if self.data.get("model_roles"):
            lines.append("   roles: " + ", ".join(f"{k}→{v}" for k, v in
                                                  self.data["model_roles"].items()))
        routing = self.data.get("routing") or {}
        if routing.get("enabled"):
            lines.append("   routing: " + ", ".join(
                f"{k}→{v}" for k, v in routing.items() if k != "enabled"))
        if not self.models:
            lines.append("   (no models yet)")
        return "\n".join(lines)

    def run(self) -> bool:
        """Returns True if the file was written."""
        while True:
            self.p.out("\n" + self.summary())
            aliases = list(self.models)
            actions = ["add a model", "test a model", "edit a model", "remove a model",
                       "set the default model", "models per role", "model pool",
                       "difficulty routing", "save and quit", "quit without saving"]
            choice = self.p.choose("\nWhat next?", actions, "add a model")
            if choice is None:
                continue
            if choice == "add a model":
                self.add_models()
            elif choice == "test a model" and aliases:
                alias = self.p.choose("Test which model?", aliases)
                if alias:
                    self.test(alias)
            elif choice == "edit a model" and aliases:
                alias = self.p.choose("Edit which model?", aliases)
                if alias:
                    self.edit(alias)
            elif choice == "remove a model" and aliases:
                alias = self.p.choose("Remove which model?", aliases)
                if alias:
                    self.remove(alias)
            elif choice == "set the default model" and aliases:
                pick = self.p.choose("Default model", aliases + list(self.data.get("pools") or {}))
                if pick:
                    self.data["default_model"] = pick
            elif choice == "models per role":
                self.set_roles()
            elif choice == "model pool":
                self.set_pool()
            elif choice == "difficulty routing":
                self.set_routing()
            elif choice == "quit without saving":
                return False
            elif choice == "save and quit":
                return self.save()

    def save(self) -> bool:
        if not self.changed():
            self.p.out("Nothing changed.")
            return False
        for ref in dangling_references(self.data):
            self.p.out(f"  note: {ref} is not defined in this file "
                       "(it may come from another config layer).")
        self.p.out("\n" + self.summary())
        if self.has_secret:
            self.p.out("  An API key will be stored in this file (mode 600). "
                       "Never commit it.")
        if not self.p.yes(f"Write {self.path}?", True):
            return False
        write_file(self.path, self.data, secret=self.has_secret)
        self.p.out(f"Saved {self.path}")
        return True


def _default_alias(model_id: str, taken: Dict[str, Any]) -> str:
    base = model_id.split("/")[-1]
    alias, n = base, 2
    while alias in taken:
        alias, n = f"{base}-{n}", n + 1
    return alias


def _coerce(field: str, value: str) -> Any:
    if field in ("context_window", "max_tokens"):
        try:
            return int(float(value))
        except ValueError:
            return value
    if field in ("temperature", "timeout"):
        try:
            return float(value)
        except ValueError:
            return value
    return value


def run_wizard(*, project: bool = False, config_path: Optional[str] = None,
               cwd: Optional[str] = None, prompter: Optional[Prompter] = None) -> int:
    """`coded config setup`. Returns a process exit code."""
    from coded import ui

    if prompter is None and not sys.stdin.isatty():
        ui.error("`coded config setup` is interactive and needs a terminal. "
                 "Edit the config file directly, or run `coded config init` "
                 "to write a sample.")
        return 1
    path = target_path(project=project, config_path=config_path, cwd=cwd)
    prompter = prompter or console_prompter()
    try:
        wizard = Wizard(path, prompter, allow_literal_key=not project)
    except ConfigError as exc:
        ui.error(str(exc))
        return 1
    prompter.out(f"Configuring models in {path}.\n"
                 "Nothing is written until you choose 'save and quit'.")
    try:
        wizard.run()
    except Abort:
        prompter.out("\nAborted; nothing was written.")
        return 1
    except ConfigError as exc:
        ui.error(str(exc))
        return 1
    return 0


def reload_into(cfg: Config, cwd: Optional[str] = None) -> Config:
    """Re-read every layer after the wizard, for a running REPL."""
    from coded.config import load_config

    return load_config(cwd=cwd)
