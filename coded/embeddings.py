"""Embeddings client and resolution for semantic search.

An embedding model is resolved from config in this order:
  1. config "embedding": { provider/model/base_url/... } inline block
  2. config "embedding_model": "<alias>" referencing an entry in `models`
  3. a default guessed from the active provider (OpenAI-style)
"""

from __future__ import annotations

from typing import List, Optional

from openai import OpenAI

from coded.config import Config, ConfigError, ModelConfig

# Reasonable default embedding model ids per provider.
_DEFAULT_EMBED = {
    "openai": "text-embedding-3-small",
    "openai-compatible": "text-embedding-3-small",
    "deepinfra": "BAAI/bge-base-en-v1.5",
    "together": "BAAI/bge-base-en-v1.5",
    "mistral": "mistral-embed",
    "ollama": "nomic-embed-text",
    "lmstudio": "nomic-embed-text",
}


def resolve_embedding_model(config: Config, active: Optional[ModelConfig] = None) -> ModelConfig:
    raw = getattr(config, "embedding", None)
    if isinstance(raw, dict):
        return ModelConfig.from_dict("embedding", raw)
    alias = getattr(config, "embedding_model", None)
    if alias:
        if alias not in config.models:
            raise ConfigError(f"embedding_model '{alias}' is not in 'models'.")
        return config.models[alias]
    # Guess from the active model's provider.
    if active is not None:
        default_id = _DEFAULT_EMBED.get(active.provider)
        if default_id:
            return ModelConfig(
                name="embedding", model=default_id, provider=active.provider,
                base_url=active.base_url, api_key=active.api_key,
                api_key_env=active.api_key_env,
            )
    raise ConfigError(
        "No embedding model configured. Add an 'embedding' block or "
        "'embedding_model' to your config (see README)."
    )


class EmbeddingClient:
    def __init__(self, model: ModelConfig):
        self.model = model
        self.client = OpenAI(
            base_url=model.resolved_base_url(),
            api_key=model.resolved_api_key(),
            default_headers=model.extra_headers or None,
        )

    def embed(self, texts: List[str]) -> List[List[float]]:
        resp = self.client.embeddings.create(model=self.model.model, input=texts)
        # Preserve input order.
        return [d.embedding for d in sorted(resp.data, key=lambda d: d.index)]
