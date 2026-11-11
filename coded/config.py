"""Configuration: providers, models, and settings.

Configuration is layered, later layers override earlier ones:

1. Built-in provider presets (base URLs + default API-key env vars).
2. User config file:  ~/.config/coded/config.json  (or $CODED_CONFIG).
3. Project config file:  ./.coded/config.json  (relative to the working dir).
4. Command-line overrides (``--model``, ``--base-url``, ``--api-key`` ...).

A "model" in coded is a named alias that points at a concrete API model id on
some provider. This lets a user register several models (local, hosted, cheap,
smart) and switch between them at runtime with ``/model``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, Optional

# ---------------------------------------------------------------------------
# Built-in provider presets.
#
# Each preset supplies a default ``base_url`` and the environment variable that
# normally holds the API key. A model entry can set ``"provider": "groq"`` and
# omit base_url / api_key to inherit these.
# ---------------------------------------------------------------------------
PROVIDERS: Dict[str, Dict[str, str]] = {
    "openai": {"base_url": "https://api.openai.com/v1", "api_key_env": "OPENAI_API_KEY"},
    # Anthropic and Google both expose OpenAI-compatible endpoints.
    "anthropic": {"base_url": "https://api.anthropic.com/v1", "api_key_env": "ANTHROPIC_API_KEY"},
    "google": {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "api_key_env": "GEMINI_API_KEY"},
    "openrouter": {"base_url": "https://openrouter.ai/api/v1", "api_key_env": "OPENROUTER_API_KEY"},
    "groq": {"base_url": "https://api.groq.com/openai/v1", "api_key_env": "GROQ_API_KEY"},
    "together": {"base_url": "https://api.together.xyz/v1", "api_key_env": "TOGETHER_API_KEY"},
    "deepseek": {"base_url": "https://api.deepseek.com/v1", "api_key_env": "DEEPSEEK_API_KEY"},
    "mistral": {"base_url": "https://api.mistral.ai/v1", "api_key_env": "MISTRAL_API_KEY"},
    "xai": {"base_url": "https://api.x.ai/v1", "api_key_env": "XAI_API_KEY"},
    "fireworks": {"base_url": "https://api.fireworks.ai/inference/v1", "api_key_env": "FIREWORKS_API_KEY"},
    "cerebras": {"base_url": "https://api.cerebras.ai/v1", "api_key_env": "CEREBRAS_API_KEY"},
    "perplexity": {"base_url": "https://api.perplexity.ai", "api_key_env": "PERPLEXITY_API_KEY"},
    "nvidia": {"base_url": "https://integrate.api.nvidia.com/v1", "api_key_env": "NVIDIA_API_KEY"},
    "deepinfra": {"base_url": "https://api.deepinfra.com/v1/openai", "api_key_env": "DEEPINFRA_API_KEY"},
    "moonshot": {"base_url": "https://api.moonshot.ai/v1", "api_key_env": "MOONSHOT_API_KEY"},
    # Local servers — key is usually unused, so a dummy is fine.
    "ollama": {"base_url": "http://localhost:11434/v1", "api_key_env": "OLLAMA_API_KEY"},
    "lmstudio": {"base_url": "http://localhost:1234/v1", "api_key_env": "LMSTUDIO_API_KEY"},
    "llamacpp": {"base_url": "http://localhost:8080/v1", "api_key_env": "LLAMACPP_API_KEY"},
    "vllm": {"base_url": "http://localhost:8000/v1", "api_key_env": "VLLM_API_KEY"},
    "jan": {"base_url": "http://localhost:1337/v1", "api_key_env": "JAN_API_KEY"},
    # Generic OpenAI-compatible endpoint; supply base_url yourself.
    "openai-compatible": {"base_url": "", "api_key_env": "OPENAI_API_KEY"},
}

# Providers whose local servers accept any API key string.
LOCAL_PROVIDERS = {"ollama", "lmstudio", "llamacpp", "vllm", "jan"}


@dataclass
class ModelConfig:
    """A named, runnable model alias."""

    name: str  # alias, e.g. "smart" or "gpt-4o"
    model: str  # concrete API model id, e.g. "gpt-4o-2024-11-20"
    provider: str = "openai"
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    api_key_env: Optional[str] = None
    max_tokens: Optional[int] = None
    temperature: Optional[float] = None
    context_window: int = 128_000
    # Optional cost tracking, USD per million tokens.
    input_cost: float = 0.0
    output_cost: float = 0.0
    extra_headers: Dict[str, str] = field(default_factory=dict)
    # Some smaller/older models do not support the ``tools`` parameter.
    supports_tools: bool = True
    # Vision-capable models accept image parts in user messages.
    supports_vision: bool = False
    # TLS: verify the server certificate (set False for self-signed, insecure).
    verify_ssl: bool = True
    # Path to a custom CA cert/bundle (PEM) to trust for this endpoint.
    ca_bundle: Optional[str] = None
    # Reasoning models: "minimal" | "low" | "medium" | "high" (sent as reasoning_effort).
    reasoning_effort: Optional[str] = None
    # Provider-specific request fields merged into the request body verbatim.
    extra_body: Dict[str, Any] = field(default_factory=dict)
    # Retries for 429/5xx/connection errors, with jittered exponential backoff.
    max_retries: int = 4
    retry_base_delay: float = 1.0
    # Seconds to wait for the endpoint (per request; for streams, between chunks).
    # None = the SDK default (10 minutes).
    timeout: Optional[float] = None
    # After this endpoint fails over to a fallback, skip it for this many seconds
    # instead of retrying it on every step (0 = always try it first).
    fallback_cooldown: float = 120.0
    # Model aliases to fall back to (in order) when this endpoint keeps failing,
    # e.g. the same model on another provider/key, or a different model.
    fallback: list = field(default_factory=list)
    # Per-model overrides of the context-scaled limits (see coded/limits.py).
    limits: Dict[str, Any] = field(default_factory=dict)
    # Max agent loop steps for this model (CLI --max-turns still wins).
    max_turns: Optional[int] = None
    # Prompt set: "auto" | "standard" | "compact" | "full" (see prompt_files.py),
    # and an optional directory of this model's own prompt files.
    prompt_profile: str = "auto"
    prompts_dir: Optional[str] = None
    # Repository overview in the system prompt: "auto" (on at >= 200k context) | true | false.
    repo_overview: Any = "auto"

    def resolved_base_url(self) -> str:
        if self.base_url:
            return self.base_url
        preset = PROVIDERS.get(self.provider, {})
        url = preset.get("base_url", "")
        if not url:
            raise ConfigError(
                f"Model '{self.name}' has no base_url and provider "
                f"'{self.provider}' has no default. Set 'base_url' in your config."
            )
        return url

    def resolved_api_key(self) -> str:
        if self.api_key:
            return self.api_key
        env_name = self.api_key_env or PROVIDERS.get(self.provider, {}).get("api_key_env")
        if env_name:
            val = os.environ.get(env_name)
            if val:
                return val
        # Local providers usually accept any string.
        if self.provider in LOCAL_PROVIDERS:
            return "local"
        # A model defined by just a URL + model name (custom/self-hosted endpoint,
        # or the generic openai-compatible provider) often needs no key. Fall back
        # to a placeholder so `{ "base_url": ..., "model": ... }` works out of the box.
        if self.base_url or self.provider == "openai-compatible":
            return os.environ.get("OPENAI_API_KEY") or "not-needed"
        raise ConfigError(
            f"No API key for model '{self.name}'. Set the '{env_name}' environment "
            f"variable, or add 'api_key' to its config entry."
        )

    def ssl_verify(self):
        """Value to pass to httpx `verify=`: a CA-bundle path, or a bool.

        Precedence: explicit ca_bundle → $CODED_CA_BUNDLE (when verifying) →
        verify_ssl flag. Returning False disables verification (insecure).
        """
        if self.ca_bundle:
            return os.path.expanduser(self.ca_bundle)
        if self.verify_ssl:
            env = os.environ.get("CODED_CA_BUNDLE")
            if env:
                return os.path.expanduser(env)
        return self.verify_ssl

    @classmethod
    def from_dict(cls, name: str, data: Dict[str, Any]) -> "ModelConfig":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        kwargs = {k: v for k, v in data.items() if k in known}
        kwargs["name"] = name
        # Default the concrete model id to the alias if not given.
        kwargs.setdefault("model", data.get("model", name))
        return cls(**kwargs)


class ConfigError(Exception):
    """Raised for invalid or missing configuration."""


@dataclass
class Config:
    models: Dict[str, ModelConfig] = field(default_factory=dict)
    default_model: Optional[str] = None
    mcp_servers: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # Embedding model for semantic search: an inline dict, or an alias in `models`.
    embedding: Optional[Dict[str, Any]] = None
    embedding_model: Optional[str] = None
    # Web-search backend config, e.g. {"backend": "tavily", "api_key": "..."}.
    web_search: Optional[Dict[str, Any]] = None
    # Behaviour toggles.
    auto_approve: bool = False  # skip permission prompts (a.k.a. "yolo")
    max_turns: int = 100  # safety cap on agent tool-loop iterations
    max_turns_override: Optional[int] = None  # --max-turns (beats per-model max_turns)
    stream: bool = True
    auto_compact: bool = True  # summarize old messages near the context limit
    compact_ratio: float = 0.8  # fraction of context_window that triggers it
    permissions: Optional[Dict[str, Any]] = None  # {"allow": [...], "deny": [...]}
    # Lifecycle hooks: {"PreToolUse": [{"matcher": "bash", "command": "..."}], ...}.
    hooks: Dict[str, Any] = field(default_factory=dict)
    # OS sandbox for bash: {"enabled": true, "network": false, "writable": [...]}.
    sandbox: Dict[str, Any] = field(default_factory=dict)
    show_reasoning: bool = False  # display streamed reasoning/thinking text
    parallel_tools: bool = True  # run independent read-only tool calls concurrently
    # Tracing: {"jsonl": "path/to/trace.jsonl", "otel": false}.
    telemetry: Dict[str, Any] = field(default_factory=dict)
    # Prompt-injection guard after untrusted content: "off" | "warn" | "strict".
    injection_guard: str = "warn"
    # Which model alias to use for each role, e.g. {"subagent": "cheap",
    # "compact": "cheap", "review": "smart", "verdict": "smart", "plan": "smart"}.
    model_roles: Dict[str, str] = field(default_factory=dict)
    # Outside coding agents (Claude Code, Codex, Gemini CLI, Aider, ...).
    external_agents: Dict[str, Any] = field(default_factory=dict)
    detect_external_agents: bool = True  # offer installed CLIs automatically
    plugins: list = field(default_factory=list)  # names of enabled plugins merged in
    plugin_warnings: list = field(default_factory=list)
    source_paths: list = field(default_factory=list)  # config files that were loaded

    def get_model(self, name: Optional[str] = None) -> ModelConfig:
        name = name or self.default_model
        if not name:
            raise ConfigError(
                "No model configured. Run `coded config init` to create a config "
                "file, set OPENAI_API_KEY, or pass --model/--base-url/--api-key."
            )
        if name not in self.models:
            raise ConfigError(
                f"Unknown model '{name}'. Known models: {', '.join(self.models) or '(none)'}"
            )
        return self.models[name]

    def model_for_role(self, role: str, default: ModelConfig) -> ModelConfig:
        """The model configured for `role` (subagent, compact, review, …), else `default`."""
        alias = self.model_roles.get(role)
        if alias and alias in self.models:
            return self.models[alias]
        return default

    def fallbacks_for(self, model: ModelConfig) -> list:
        return [self.models[a] for a in model.fallback if a in self.models and a != model.name]

    def model_warnings(self) -> list:
        """Config mistakes that would otherwise be skipped silently: unknown
        aliases in `fallback` / `model_roles` / `default_model`."""
        known = ", ".join(self.models) or "(none)"
        out = []
        if self.default_model and self.default_model not in self.models:
            out.append(f"default_model '{self.default_model}' is not a configured model "
                       f"(known: {known}).")
        for mc in self.models.values():
            for alias in mc.fallback:
                if alias == mc.name:
                    out.append(f"Model '{mc.name}': fallback lists itself (ignored).")
                elif alias not in self.models:
                    out.append(f"Model '{mc.name}': fallback '{alias}' is not a configured "
                               f"model (ignored; known: {known}).")
        for role, alias in self.model_roles.items():
            if alias not in self.models:
                out.append(f"model_roles: '{role}' points to '{alias}', which is not a "
                           f"configured model (the main model is used; known: {known}).")
        return out

    def add_model(self, mc: ModelConfig, make_default: bool = False) -> None:
        self.models[mc.name] = mc
        if make_default or self.default_model is None:
            self.default_model = mc.name


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def user_config_path() -> Path:
    env = os.environ.get("CODED_CONFIG")
    if env:
        return Path(env).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
    return Path(base) / "coded" / "config.json"


def project_config_path(cwd: Optional[str] = None) -> Path:
    return Path(cwd or os.getcwd()) / ".coded" / "config.json"


def _load_json(path: Path) -> Dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Invalid JSON in {path}: {exc}") from exc


def _merge_into(cfg: Config, data: Dict[str, Any], source: Path) -> None:
    if not data:
        return
    cfg.source_paths.append(str(source))
    for name, mdata in (data.get("models") or {}).items():
        cfg.models[name] = ModelConfig.from_dict(name, mdata)
    if data.get("default_model"):
        cfg.default_model = data["default_model"]
    for name, sdata in (data.get("mcp_servers") or {}).items():
        cfg.mcp_servers[name] = sdata
    if data.get("embedding"):
        cfg.embedding = data["embedding"]
    if data.get("embedding_model"):
        cfg.embedding_model = data["embedding_model"]
    if data.get("web_search"):
        cfg.web_search = data["web_search"]
    if data.get("permissions"):
        cfg.permissions = data["permissions"]
    for key in ("hooks", "sandbox", "telemetry", "external_agents", "model_roles"):
        if data.get(key):
            merged = dict(getattr(cfg, key))
            merged.update(data[key])
            setattr(cfg, key, merged)
    for key in ("auto_approve", "max_turns", "stream", "auto_compact", "compact_ratio",
                "show_reasoning", "parallel_tools", "injection_guard",
                "detect_external_agents"):
        if key in data:
            setattr(cfg, key, data[key])


def load_config(cwd: Optional[str] = None, config_path: Optional[str] = None,
                plugins: bool = True) -> Config:
    """Load and merge all configuration layers, then enabled plugins' hooks,
    MCP servers and outside agents (user config wins on name clashes)."""
    cfg = Config()

    if config_path:
        _merge_into(cfg, _load_json(Path(config_path).expanduser()), Path(config_path))
    else:
        _merge_into(cfg, _load_json(user_config_path()), user_config_path())
        _merge_into(cfg, _load_json(project_config_path(cwd)), project_config_path(cwd))

    if plugins:
        from coded.plugins import apply_plugins_to_config

        cfg.plugin_warnings = apply_plugins_to_config(cfg, cwd)

    # Fallback: if nothing is configured but OPENAI_API_KEY exists, register a
    # sensible default so the tool works out of the box.
    if not cfg.models and os.environ.get("OPENAI_API_KEY"):
        cfg.add_model(
            ModelConfig(
                name="gpt-4o",
                model="gpt-4o",
                provider="openai",
                input_cost=2.5,
                output_cost=10.0,
            ),
            make_default=True,
        )

    return cfg


def sample_config() -> Dict[str, Any]:
    """A starter config demonstrating per-model configuration fields.

    For a large, ready-to-trim catalogue of models across many providers, see
    config.example.json in the repository.
    """
    return {
        "default_model": "gpt-4o",
        "models": {
            "gpt-4o": {
                "provider": "openai",
                "model": "gpt-4o",
                "context_window": 128000,
                "max_tokens": 16384,
                "temperature": 0.2,
                "input_cost": 2.5,
                "output_cost": 10.0,
            },
            "gpt-4o-mini": {
                "provider": "openai",
                "model": "gpt-4o-mini",
                "context_window": 128000,
                "max_tokens": 16384,
                "input_cost": 0.15,
                "output_cost": 0.6,
            },
            "claude-sonnet": {
                "provider": "anthropic",
                "model": "claude-3-7-sonnet-latest",
                "context_window": 200000,
                "max_tokens": 8192,
                "input_cost": 3.0,
                "output_cost": 15.0,
            },
            "llama-groq": {
                "provider": "groq",
                "model": "llama-3.3-70b-versatile",
                "context_window": 128000,
                "max_tokens": 32768,
            },
            "deepseek": {
                "provider": "deepseek",
                "model": "deepseek-chat",
                "context_window": 64000,
                "max_tokens": 8192,
            },
            "local": {
                "provider": "ollama",
                "model": "qwen2.5-coder:7b",
                "context_window": 32768,
                "max_tokens": 8192,
                "temperature": 0.2,
            },
        },
        "mcp_servers": {
            "_example_filesystem": {
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-filesystem", "."],
                "enabled": False,
            }
        },
        "auto_approve": False,
        "max_turns": 100,
        "stream": True,
    }


def write_sample_config(path: Optional[Path] = None) -> Path:
    path = path or user_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise ConfigError(f"Config already exists at {path}; refusing to overwrite.")
    with path.open("w", encoding="utf-8") as fh:
        json.dump(sample_config(), fh, indent=2)
    return path


def model_from_overrides(
    *,
    model_name: Optional[str],
    provider: Optional[str],
    base_url: Optional[str],
    api_key: Optional[str],
    alias: str = "cli",
) -> ModelConfig:
    """Build an ad-hoc model from command-line flags."""
    return ModelConfig(
        name=alias,
        model=model_name or "gpt-4o",
        provider=provider or "openai-compatible",
        base_url=base_url,
        api_key=api_key,
    )
