"""Runtime configuration, resolved from defaults < environment < CLI flags."""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path


def _env(name: str, default, cast=str):
    raw = os.environ.get(f"RECALLHOOK_{name}")
    return default if raw is None else cast(raw)


def _bool(v: str) -> bool:
    return v.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Config:
    # Where requests are forwarded: normally the Headroom proxy.
    upstream: str = field(default_factory=lambda: _env("UPSTREAM", "http://127.0.0.1:8787"))
    # Used only when the upstream (Headroom) refuses connections, so Claude keeps working.
    fallback: str = field(default_factory=lambda: _env("FALLBACK", "https://api.anthropic.com"))
    host: str = field(default_factory=lambda: _env("HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _env("PORT", 8788, int))

    # Local models served by Ollama.
    ollama_url: str = field(default_factory=lambda: _env("OLLAMA_URL", "http://localhost:11434"))
    llm_model: str = field(default_factory=lambda: _env("LLM_MODEL", "qwen3:8b"))
    embed_model: str = field(default_factory=lambda: _env("EMBED_MODEL", "nomic-embed-text:latest"))
    embed_dims: int = field(default_factory=lambda: _env("EMBED_DIMS", 768, int))

    # Ollama tuning. mem0 exposes none of these, so we inject them per request.
    num_ctx: int = field(default_factory=lambda: _env("NUM_CTX", 4096, int))
    keep_alive: str = field(default_factory=lambda: _env("KEEP_ALIVE", "30m"))
    embed_on_cpu: bool = field(default_factory=lambda: _env("EMBED_ON_CPU", True, _bool))

    store_path: str = field(
        default_factory=lambda: _env("STORE", str(Path.home() / ".recallhook"))
    )
    user_id: str = field(default_factory=lambda: _env("USER_ID", "default"))

    top_k: int = field(default_factory=lambda: _env("TOP_K", 5, int))
    min_score: float = field(default_factory=lambda: _env("MIN_SCORE", 0.5, float))
    search_timeout: float = field(default_factory=lambda: _env("SEARCH_TIMEOUT", 3.0, float))
    inject: bool = field(default_factory=lambda: _env("INJECT", True, _bool))
    write: bool = field(default_factory=lambda: _env("WRITE", True, _bool))
    queue_size: int = field(default_factory=lambda: _env("QUEUE_SIZE", 16, int))
    min_chars: int = 20  # ignore trivially short user turns ("ok", "continue")

    @property
    def home(self) -> Path:
        return Path(self.store_path).expanduser()

    def public(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}
