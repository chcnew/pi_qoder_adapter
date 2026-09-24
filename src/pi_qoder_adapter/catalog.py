"""Static Qoder model catalog and the Pi provider snippet."""

from __future__ import annotations

import json
from typing import Any

DEFAULT_CONTEXT_WINDOW = 200_000
DEFAULT_MAX_TOKENS = 32_000

DEFAULT_MODEL_IDS: tuple[tuple[str, str], ...] = (
    ("auto", "Qoder Auto"),
    ("ultimate", "Qoder Ultimate"),
    ("performance", "Qoder Performance"),
    ("efficient", "Qoder Efficient"),
    ("lite", "Qoder Lite"),
)


def default_pi_models() -> list[dict[str, Any]]:
    models: list[dict[str, Any]] = []
    for model_id, name in DEFAULT_MODEL_IDS:
        models.append(
            {
                "id": model_id,
                "name": name,
                "reasoning": True,
                "input": ["text"],
                "contextWindow": DEFAULT_CONTEXT_WINDOW,
                "maxTokens": DEFAULT_MAX_TOKENS,
                "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
            }
        )
    return models


def provider_config(port: int) -> dict[str, Any]:
    return {
        "name": "Qoder",
        "baseUrl": f"http://127.0.0.1:{port}/v1",
        "api": "openai-completions",
        "compat": {
            "supportsDeveloperRole": True,
            "supportsReasoningEffort": False,
            "supportsUsageInStreaming": True,
            "maxTokensField": "max_tokens",
        },
        "models": default_pi_models(),
    }


def render_pi_config(port: int) -> str:
    """JSON fragment to merge into Pi's models.json. The PAT is not included."""
    return json.dumps(
        {"providers": {"qoder": provider_config(port)}},
        ensure_ascii=False,
        indent=2,
    )


def context_window_from_info(info: dict[str, Any]) -> int:
    config = info.get("context_config")
    counts: list[int] = []
    if isinstance(config, dict):
        for entry in config.values():
            if isinstance(entry, dict) and isinstance(entry.get("token_count"), int):
                counts.append(entry["token_count"])
    return max(counts) if counts else DEFAULT_CONTEXT_WINDOW


def reasoning_from_info(info: dict[str, Any]) -> bool:
    thinking = info.get("thinking_config")
    if not isinstance(thinking, dict):
        return True
    enabled = thinking.get("enabled")
    return isinstance(enabled, dict)


def model_alias_key(value: str) -> str:
    """Compare model ids and display names without case or punctuation."""
    return "".join(ch for ch in value.casefold() if ch.isalnum())


def resolve_model_id(requested: str, models: list[dict[str, Any]]) -> str | None:
    """Map a Pi model id or a Qoder display name onto a catalog id."""
    wanted = requested.strip()
    if not wanted:
        return None
    for model in models:
        if model.get("id") == wanted:
            return wanted
    key = model_alias_key(wanted)
    matches: list[str] = []
    for model in models:
        model_id = model.get("id")
        if not isinstance(model_id, str) or not model_id:
            continue
        names = [model_id, model.get("name")]
        if any(isinstance(name, str) and name and model_alias_key(name) == key for name in names):
            matches.append(model_id)
    unique = list(dict.fromkeys(matches))
    if len(unique) == 1:
        return unique[0]
    return None


def pi_model_from_info(info: dict[str, Any]) -> dict[str, Any] | None:
    model_id = info.get("value")
    if not isinstance(model_id, str) or not model_id:
        return None
    if info.get("isEnabled") is False:
        return None
    name = info.get("displayName")
    return {
        "id": model_id,
        "name": name if isinstance(name, str) and name else model_id,
        "reasoning": reasoning_from_info(info),
        "input": ["text"],
        "contextWindow": context_window_from_info(info),
        "maxTokens": DEFAULT_MAX_TOKENS,
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
    }


def openai_model_list(models: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "object": "list",
        "data": [
            {
                "id": model["id"],
                "object": "model",
                "owned_by": "qoder",
                "context_window": model.get("contextWindow", DEFAULT_CONTEXT_WINDOW),
                "max_tokens": model.get("maxTokens", DEFAULT_MAX_TOKENS),
            }
            for model in models
        ],
    }
