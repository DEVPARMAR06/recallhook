"""Runtime configuration. Precedence: defaults < ~/.recallhook/config.json < RECALLHOOK_* env < CLI flags."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields
from pathlib import Path


def home_dir() -> Path:
    return Path(os.environ.get("RECALLHOOK_HOME") or Path.home() / ".recallhook")


def config_path() -> Path:
    return home_dir() / "config.json"


def read_file() -> dict:
    try:
        data = json.loads(config_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _bool(v) -> bool:
    return v if isinstance(v, bool) else str(v).strip().lower() in {"1", "true", "yes", "on"}


def _env(name: str, default, cast=str):
    raw = os.environ.get(f"RECALLHOOK_{name}")
    if raw is not None:
        return cast(raw)
    stored = read_file().get(name.lower())
    if stored is not None:
        try:
            return cast(stored)
        except (TypeError, ValueError):
            pass
    return default


# Settings users may change with `recallhook config set`, and their types.
SETTABLE: dict[str, type] = {
    "llm_model": str, "embed_model": str, "embed_dims": int, "ollama_url": str, "port": int,
    "num_ctx": int, "keep_alive": str, "embed_on_cpu": bool, "user_id": str, "top_k": int,
    "min_score": float, "search_timeout": float, "inject": bool, "write": bool,
}
_RANGES = {"port": (1, 65535), "top_k": (1, 50), "min_score": (0.0, 1.0), "embed_dims": (1, 16384),
           "num_ctx": (512, 131072), "search_timeout": (0.1, 60.0)}


def parse_setting(key: str, text: str):
    """Validate and convert a `config set` value. Raises ValueError with a helpful message."""
    if key not in SETTABLE:
        raise ValueError(f"unknown setting '{key}'. Settable: {', '.join(sorted(SETTABLE))}")
    kind = SETTABLE[key]
    if kind is bool:
        if text.strip().lower() not in {"1", "0", "true", "false", "yes", "no", "on", "off"}:
            raise ValueError(f"{key} must be true or false")
        value = _bool(text)
    else:
        try:
            value = kind(text)
        except ValueError:
            raise ValueError(f"{key} must be a {kind.__name__}") from None
    if key in _RANGES and not _RANGES[key][0] <= value <= _RANGES[key][1]:
        raise ValueError(f"{key} must be between {_RANGES[key][0]} and {_RANGES[key][1]}")
    return value


def update_file(key: str, value) -> None:
    """Set (or, with value None, remove) one key in the config file."""
    data = read_file()
    if value is None:
        data.pop(key, None)
    else:
        data[key] = value
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def effective_port() -> int:
    """Gateway port as the hook should see it (env, then config file, then 8788)."""
    try:
        return int(os.environ.get("RECALLHOOK_PORT") or read_file().get("port") or 8788)
    except (TypeError, ValueError):
        return 8788


@dataclass
class Config:
    # Optional Headroom proxy mode (off by default): where requests are forwarded.
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

    store_path: str = field(default_factory=lambda: _env("STORE", str(home_dir())))
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
