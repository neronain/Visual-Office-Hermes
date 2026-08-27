"""What the gateway says each model can do, and which one the agent runs on.

Desks name a model; the model's own settings — which machine serves it, how big
its context is, whether it can read an image — live in the gateway, one entry
per model. That is the right place for them: the gateway is what actually routes
the call, and it measured those capabilities against the running backend.

This module reads that catalogue so the office can show it, and applies a change
of main model back to ``~/.hermes/config.yaml``. It never invents a capability;
everything shown comes from the gateway's own answer.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

CATALOG_TTL_SECONDS = 60.0
_CACHE: dict[str, Any] = {"at": 0.0, "models": []}


def config_path() -> Path:
    home = os.environ.get("HERMES_HOME", "").strip()
    base = Path(home) if home else Path.home() / ".hermes"
    return base / "config.yaml"


def _load_config() -> dict[str, Any]:
    try:
        import yaml

        return yaml.safe_load(config_path().read_text(encoding="utf-8")) or {}
    except Exception as exc:
        logger.debug("visual_office could not read config.yaml: %s", exc)
        return {}


def _env_file_value(name: str) -> str:
    """Read one name out of ~/.hermes/.env.

    Hermes loads that file into the environment at startup, so normally
    os.environ already has it. Reading the file as a fallback keeps this working
    from a plain python process and from any startup order.
    """
    path = config_path().with_name(".env")
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            if key.strip() == name:
                return value.strip().strip("'\"")
    except OSError:
        pass
    return ""


def _resolve_key(value: Any) -> str:
    """Config stores ``${SOME_ENV}``; the value itself lives outside the file."""
    text = str(value or "").strip()
    if not (text.startswith("${") and text.endswith("}")):
        return text
    name = text[2:-1]
    return os.environ.get(name, "").strip() or _env_file_value(name)


# ---------------------------------------------------------------------------
# Which live conversation a command from the office should land in
# ---------------------------------------------------------------------------


def known_channels() -> list[dict[str, Any]]:
    """Chats Hermes has seen, from the directory the gateway keeps up to date."""
    path = config_path().with_name("channel_directory.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    out = []
    for platform, entries in (data.get("platforms") or {}).items():
        for entry in entries or []:
            if isinstance(entry, dict) and entry.get("id"):
                out.append({
                    "platform": str(platform),
                    "chat_id": str(entry["id"]),
                    "chat_type": str(entry.get("type") or "dm"),
                    "thread_id": entry.get("thread_id") or None,
                    "name": str(entry.get("name") or ""),
                })
    return out


def session_key_for(channel: dict[str, Any]) -> str:
    """Build the gateway session key for a channel.

    Uses Hermes' own ``build_session_key`` — it calls itself the single source
    of truth for this, and a second implementation here would drift the first
    time the rules change.
    """
    try:
        from gateway.session import Platform, SessionSource, build_session_key

        source = SessionSource(
            platform=Platform(channel["platform"]),
            chat_id=channel["chat_id"],
            chat_type=channel.get("chat_type") or "dm",
            thread_id=channel.get("thread_id"),
        )
        return build_session_key(source)
    except Exception as exc:
        logger.debug("visual_office could not build a session key: %s", exc)
        return ""


def agent_model() -> tuple[str, str, str]:
    """``(model, base_url, api_key)`` the top-level agent is configured with."""
    model_cfg = _load_config().get("model") or {}
    return (
        str(model_cfg.get("default") or "").strip(),
        str(model_cfg.get("base_url") or "").strip().rstrip("/"),
        _resolve_key(model_cfg.get("api_key")),
    )


def catalog(force: bool = False) -> list[dict[str, Any]]:
    """Models this agent's key can actually call, with the gateway's own flags.

    Cached briefly: the office asks on every roster announce, and the answer
    changes only when an operator edits the gateway.
    """
    now = time.monotonic()
    if not force and _CACHE["models"] and (now - _CACHE["at"]) < CATALOG_TTL_SECONDS:
        return _CACHE["models"]

    _, base_url, api_key = agent_model()
    if not base_url:
        return []

    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        request = urllib.request.Request(f"{base_url}/models", headers=headers)
        with urllib.request.urlopen(request, timeout=8) as response:
            body = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        logger.debug("visual_office could not read the gateway catalog: %s", exc)
        return _CACHE["models"]

    models = []
    for entry in body.get("data") or []:
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        caps = entry.get("capabilities") or {}
        models.append({
            "id": str(entry["id"]),
            "display_name": str(entry.get("display_name") or entry["id"]),
            "description": str(entry.get("description") or ""),
            "context_window": entry.get("context_window"),
            "max_output_tokens": entry.get("max_output_tokens"),
            "capabilities": {
                k: bool(caps.get(k))
                for k in ("vision", "tools", "reasoning", "streaming", "coding")
            },
            "protocols": list(entry.get("protocols") or []),
        })
    models.sort(key=lambda m: m["id"])
    _CACHE["models"] = models
    _CACHE["at"] = now
    return models


# There is deliberately no setter here. Changing which model the top-level agent
# runs on is done by its owner — `hermes model`, or editing ~/.hermes/config.yaml
# — not by this plugin as a side effect of something else.
