"""Plain-language checks used by `recallhook status`, `init` and the dashboard banner."""
from __future__ import annotations

import json
import urllib.request
from typing import Callable

from .config import Config
from .store import StoreMismatch, check as check_store


def normalize(model: str) -> str:
    return model if ":" in model else f"{model}:latest"


def installed_models(cfg: Config, timeout: float = 3.0) -> set[str] | None:
    """Names of models Ollama has downloaded, or None if Ollama is unreachable."""
    try:
        with urllib.request.urlopen(cfg.ollama_url.rstrip("/") + "/api/tags", timeout=timeout) as r:
            return {m["name"] for m in json.loads(r.read()).get("models", [])}
    except Exception:  # noqa: BLE001
        return None


def pull_model(cfg: Config, model: str, say: Callable[[str], None] = print) -> None:
    """Download a model through the Ollama API (no `ollama` CLI needed), printing progress."""
    import ollama

    last = -10
    for part in ollama.Client(host=cfg.ollama_url).pull(model, stream=True):
        total, done = part.get("total"), part.get("completed")
        if total and done is not None:
            pct = int(100 * done / total)
            if pct >= last + 10:
                last = pct
                say(f"  {model}: {pct}%")
        elif part.get("status") and last < 0:
            say(f"  {model}: {part['status']}")


def check(cfg: Config) -> list[str]:
    """Everything that would stop recall/capture working, each with how to fix it."""
    names = installed_models(cfg)
    if names is None:
        return [f"Ollama is not reachable at {cfg.ollama_url}. Install it from https://ollama.com "
                f"and make sure it is running."]
    have = {normalize(n) for n in names}
    problems = [f"Model '{m}' is not downloaded. Run: ollama pull {m}"
                for m in dict.fromkeys((cfg.llm_model, cfg.embed_model)) if normalize(m) not in have]
    try:
        check_store(cfg, write=False)
    except StoreMismatch as e:
        problems.append(str(e))
    return problems
