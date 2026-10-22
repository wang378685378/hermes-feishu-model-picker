# Hermes plugin: Feishu Model Picker
#
# Adds a two-tier model picker for Feishu bot custom menus.
#
# Trigger: user clicks custom bot menu item -> Feishu sends text
#   "/model pick <provider-slug>"  (e.g. "/model pick myprovider")
#   -> pre_gateway_dispatch hook intercepts -> sends Feishu interactive card
#      with model buttons -> skip normal dispatch.
#
# Selection: user clicks a model button -> Feishu POSTs p2_card_action_trigger
#   -> adapter's _handle_card_action_event() synthesizes "/card <tag> <json>"
#   -> pre_gateway_dispatch hook intercepts -> calls switch_model() via
#      asyncio.run_coroutine_threadsafe on the gateway's loop -> sends ack card
#      -> skip normal dispatch.
#
# Global switch: writes config.yaml via switch_model(is_global=True) AND
# updates the in-memory cached agent so the next turn uses the new model.
# No restart needed.
#
# Multi-profile: each Feishu-connected profile gets its own gateway process
# with its own HERMES_HOME, so this plugin picks up that profile's config.yaml.

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("plugin.feishu-model-picker")
# Ensure plugin logs surface in gateway.log (INFO level). The gateway's root
# logger already attaches a handler; this just sets our own level.
if logger.level == logging.NOTSET:
    logger.setLevel(logging.INFO)
# Also ensure parent "plugin" logger propagates.
logging.getLogger("plugin").setLevel(logging.INFO)

# Default HERMES_HOME when the env var is unset. The gateway always sets
# HERMES_HOME in practice; this is only a last-resort fallback.
_DEFAULT_HERMES_HOME = os.path.expanduser("~/.hermes")

# Optional built-in display-name aliases for specific model IDs. Empty by
# default — the picker shows raw model IDs. You can populate this, or (better)
# use display_names.json / FEISHU_PICKER_MODEL_DISPLAY_NAMES so changes take
# effect without editing the plugin. See _display_name() — aliases are only
# applied if you re-enable them there.
#
# The external file lets you change labels without restarting the gateway:
#   echo '{"my-model-id":"🔥 My Model"}' > \
#     $HERMES_HOME/plugins/feishu-model-picker/display_names.json
_MODEL_LABELS: Dict[str, str] = {}

# Emoji per provider slug (the bare slug after any "custom:" prefix).
# Extend with your own provider slugs; unknown slugs fall back to 🔌.
_PROVIDER_EMOJI: Dict[str, str] = {
    "hermes-studio": "🏛️",
    "anthropic": "🤖",
    "openai": "🔵",
    "deepseek": "💎",
    "moonshot": "🌙",
    "google": "🔷",
}

# Friendly provider labels for the all-providers card. Keyed by the provider's
# config `name` (lowercased). Unknown names fall back to "🔌 <name>".
_PROVIDER_DISPLAY_NAMES: Dict[str, str] = {}

# Chinese (and other) aliases for Feishu menu items / free-text triggers.
# Maps a cleaned menu name (lowercased, emoji stripped) to the command the
# plugin should run. Two kinds of value:
#   - "/mp <provider>"  -> open that provider's model picker card
#   - "/stop" etc.      -> rewrite to a built-in gateway slash command
# Edit freely for your own menu labels / providers.
_CHINESE_ALIASES: Dict[str, str] = {
    "停止操作": "/stop",
    "压缩上下文": "/compress",
    "查看用量": "/usage",
    "状态": "/status",
    "重试": "/retry",
}

# Model buttons per row (Feishu supports max 5).
_BUTTONS_PER_ROW = 5

# /model pick <provider> | /mp <provider> | /model-picker <provider>
# Colons allowed so "/model-picker custom:myprovider" works too.
_TRIGGER_RE = re.compile(
    r"^/(?:model\s+pick|mp|model-picker)\s+([a-z0-9_\-:]+)\s*$",
    re.IGNORECASE,
)

# /model refresh <provider> | /mr <provider> — fetch live model list from the
# provider's /v1/models endpoint and write it back to config.yaml.
_REFRESH_RE = re.compile(
    r"^/(?:model\s+refresh|mr|model-refresh)\s+([a-z0-9_\-:]+)\s*$",
    re.IGNORECASE,
)

# "更换模型" / "切换模型" / "换模型" / "change model" / bare "/model" /
# bare "/models" / bare "/model pick" — open the ALL-providers picker card.
_ALL_TRIGGER_RE = re.compile(
    r"^(?:更换模型|切换模型|换模型|change\s+model|"
    r"/models?|/model\s+pick\s*)$",
    re.IGNORECASE,
)

# Regex for the "/card <tag> <json>" that the Feishu adapter's
# _handle_card_action_event synthesizes from a card button click.
_CARD_RE = re.compile(r"^/card\s+([^\s]+)\s+(\{.*\})\s*$", re.DOTALL)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_feishu(source: Any) -> bool:
    """Return True if the event source is Feishu/Lark."""
    platform = getattr(source, "platform", None)
    if platform is None:
        return False
    val = getattr(platform, "value", platform)
    try:
        val_str = str(val).lower()
    except Exception:
        val_str = ""
    return val_str in ("feishu", "lark")


def _display_name(model_id: str) -> str:
    """Return the complete raw model ID for the Feishu button label.

    The picker deliberately does not abbreviate, title-case, truncate, or
    apply display-name aliases. This keeps similar model variants distinct.
    """
    return model_id


# ---------------------------------------------------------------------------
# Hot-reloadable display names
#
# File: $HERMES_HOME/plugins/feishu-model-picker/display_names.json
# Shape: {"model-id": "display label", ...}
# Env override: FEISHU_PICKER_MODEL_DISPLAY_NAMES='{"mid":"label"}'
#
# All IO is wrapped in try/except — a bad file silently falls back to
# _MODEL_LABELS. The gateway never crashes on bad config.
# ---------------------------------------------------------------------------

_external_names_cache: Dict[str, Any] = {
    "mtime": 0.0,
    "size": -1,
    "value": {},
}
_external_names_cache_lock = threading.Lock()


def _display_names_file() -> str:
    home = os.environ.get("HERMES_HOME", _DEFAULT_HERMES_HOME)
    return os.path.join(home, "plugins", "feishu-model-picker",
                        "display_names.json")


def _reload_external_names() -> Dict[str, str]:
    """Load display_names.json + env var. Cached by (mtime, size)."""
    path = _display_names_file()
    env_var = os.environ.get("FEISHU_PICKER_MODEL_DISPLAY_NAMES", "")
    merged: Dict[str, str] = {}

    # 1. JSON file with mtime/size cache.
    try:
        st = os.stat(path)
        cache_key = (st.st_mtime, st.st_size)
        if cache_key != (_external_names_cache["mtime"],
                          _external_names_cache["size"]):
            with _external_names_cache_lock:
                with open(path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                if not isinstance(raw, dict):
                    raw = {}
                clean = {
                    k: v for k, v in raw.items()
                    if isinstance(k, str) and isinstance(v, str) and v
                }
                _external_names_cache.update({
                    "mtime": st.st_mtime,
                    "size": st.st_size,
                    "value": clean,
                })
        merged.update(_external_names_cache.get("value", {}))
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning("[ModelPicker] display_names.json reload failed: %s", e)

    # 2. Env var JSON (parsed each call — cheap).
    if env_var:
        try:
            env_override = json.loads(env_var)
            if isinstance(env_override, dict):
                for k, v in env_override.items():
                    if isinstance(k, str) and isinstance(v, str) and v:
                        merged[k] = v
        except Exception as e:
            logger.warning(
                "[ModelPicker] FEISHU_PICKER_MODEL_DISPLAY_NAMES parse failed: %s",
                e,
            )

    return merged


def _get_display_name_override(model_id: str) -> str:
    """Return external override for model_id, or '' if none."""
    if not model_id:
        return ""
    try:
        return _reload_external_names().get(model_id, "") or ""
    except Exception:
        return ""


def _provider_emoji(provider_slug: str) -> str:
    return _PROVIDER_EMOJI.get(provider_slug.split(":")[-1], "🔌")


def _resolve_provider_slug(raw: str) -> str:
    """Normalize a provider slug.

    Accepts: 'myprovider', 'custom:myprovider'. Returns 'custom:<slug>'
    unless the slug is a known built-in provider.
    """
    slug = raw.strip().lower()
    if slug.startswith("custom:"):
        return slug
    builtins = {"anthropic", "openai", "google", "deepseek", "moonshot",
                "hermes-studio", "hermesstudio", "custom"}
    if slug in builtins:
        return slug
    return f"custom:{slug}"


def _profile_config_path() -> str:
    """Return the current profile's config.yaml path.

    Uses $HERMES_HOME/config.yaml — which points at whichever profile's
    gateway process is running (each profile gateway has its own HERMES_HOME).
    """
    home = os.environ.get("HERMES_HOME", _DEFAULT_HERMES_HOME)
    return os.path.join(home, "config.yaml")


def _load_profile_config() -> Dict[str, Any]:
    """Read the current profile's config.yaml as dict."""
    import yaml
    path = _profile_config_path()
    if not os.path.exists(path):
        path = os.path.join(_DEFAULT_HERMES_HOME, "config.yaml")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        logger.warning("[ModelPicker] Failed to read %s: %s", path, e)
        return {}


def _list_models_for_provider(provider_slug: str) -> List[str]:
    """Return the model ids for a provider from config.yaml."""
    cfg = _load_profile_config()
    models: List[str] = []
    target = provider_slug.strip().lower()
    bare = target.split(":", 1)[-1]
    for cp in cfg.get("custom_providers", []) or []:
        if not isinstance(cp, dict):
            continue
        cp_prov = (cp.get("provider") or "").strip().lower()
        cp_name = (cp.get("name") or "").strip().lower()
        if (cp_prov == target or cp_prov == bare
                or cp_name == bare or cp_name == target):
            raw = cp.get("models", []) or []
            if isinstance(raw, dict):
                for m in raw.keys():
                    if isinstance(m, str) and m and m not in models:
                        models.append(m)
            elif isinstance(raw, list):
                for m in raw:
                    if isinstance(m, str) and m and m not in models:
                        models.append(m)
                    elif isinstance(m, dict):
                        mid = m.get("id")
                        if isinstance(mid, str) and mid and mid not in models:
                            models.append(mid)
    return models


def _current_model_and_provider() -> Tuple[str, str]:
    cfg = _load_profile_config()
    model_blk = cfg.get("model", {}) or {}
    return (
        str(model_blk.get("default", "") or "").strip(),
        str(model_blk.get("provider", "") or "").strip(),
    )


def _provider_key(value: str) -> str:
    """Normalize a provider slug for comparison: drop the 'custom:' prefix."""
    return (value or "").strip().lower().split(":", 1)[-1]


def _current_model_for_provider(provider_slug: str) -> str:
    """Return the model a provider is currently using, else ''."""
    cur_model, cur_provider = _current_model_and_provider()
    if not cur_model:
        return ""
    return cur_model if _provider_key(cur_provider) == _provider_key(provider_slug) else ""


# ---------------------------------------------------------------------------


def _find_custom_provider(provider_slug: str) -> Optional[Dict[str, Any]]:
    """Return the custom_providers[] entry matching provider_slug, or None."""
    cfg = _load_profile_config()
    target = (provider_slug or "").strip().lower()
    bare = target.split(":", 1)[-1]
    for cp in cfg.get("custom_providers", []) or []:
        if not isinstance(cp, dict):
            continue
        cp_prov = (cp.get("provider") or "").strip().lower()
        cp_name = (cp.get("name") or "").strip().lower()
        if cp_prov in (target, bare) or cp_name in (target, bare):
            return cp
    return None


def _fetch_provider_models(provider_slug: str,
                           timeout: float = 10.0) -> Tuple[bool, Any]:
    """GET <base_url>/models and return (ok, either list[str] or error msg).

    Uses urllib from stdlib so the plugin doesn't depend on requests/aiohttp.
    Reads API key from the entry's key_env variable in the process env.
    """
    import os
    import urllib.request
    import urllib.error

    cp = _find_custom_provider(provider_slug)
    if cp is None:
        return False, f"unknown provider: {provider_slug}"

    base_url = str(cp.get("base_url") or "").rstrip("/")
    if not base_url:
        return False, "provider has no base_url"

    # OpenAI-compatible endpoint: /v1/models (base_url already ends with /v1
    # in the current config.yaml entries, so we append "/models").
    # Strip trailing "/v1" first to be robust.
    if base_url.endswith("/v1"):
        base_url = base_url[:-3]
    models_url = base_url + "/v1/models"

    key_env = cp.get("key_env") or ""
    api_key = os.environ.get(key_env, "") if key_env else ""
    if not api_key:
        # Some endpoints allow unauth listing; try without auth.
        logger.warning("[ModelPicker] No API key for %s (env=%s); trying unauth", provider_slug, key_env)

    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(models_url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.reason}"
    except urllib.error.URLError as e:
        return False, f"URL error: {e.reason}"
    except Exception as e:
        return False, f"fetch failed: {e}"

    try:
        data = json.loads(body)
    except Exception as e:
        return False, f"invalid JSON: {e}"

    # OpenAI-compatible shape: {"data": [{"id": "...", ...}, ...]}
    entries = data.get("data") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return False, f"unexpected response shape: {str(data)[:200]}"

    ids: List[str] = []
    for entry in entries:
        if isinstance(entry, dict):
            mid = entry.get("id")
            if isinstance(mid, str) and mid and mid not in ids:
                ids.append(mid)
    if not ids:
        return False, "empty model list from provider"
    return True, ids


def _persist_provider_models(provider_slug: str,
                             new_ids: List[str]) -> bool:
    """Write discovered model ids back to config.yaml under custom_providers[]."""
    try:
        from hermes_cli.config import read_user_config_raw, save_config
        cfg = read_user_config_raw(_profile_config_path())
        target = (provider_slug or "").strip().lower()
        bare = target.split(":", 1)[-1]
        updated = False
        for cp in cfg.get("custom_providers", []) or []:
            if not isinstance(cp, dict):
                continue
            cp_prov = (cp.get("provider") or "").strip().lower()
            cp_name = (cp.get("name") or "").strip().lower()
            if cp_prov in (target, bare) or cp_name in (target, bare):
                # Store as dict of ids -> {} (matches current shape).
                cp["models"] = {mid: {} for mid in new_ids}
                cp["models_discovered"] = True
                cp["models_discovered_at"] = _now_iso()
                updated = True
                break
        if not updated:
            logger.warning("[ModelPicker] _persist_provider_models: provider %s not found", provider_slug)
            return False
        save_config(cfg)
        return True
    except Exception as e:
        logger.warning("[ModelPicker] Persist models failed: %s", e)
        return False


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Card builders
# ---------------------------------------------------------------------------

_CARD_SIGNATURE = "— Hermes · 模型切换"


def _build_refresh_error_card(provider_slug: str, error: str) -> Dict[str, Any]:
    """Build an error card for a failed model refresh."""
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"❌ 刷新 {provider_slug} 失败"},
            "template": "red",
        },
        "elements": [
            {"tag": "markdown", "content": f"**错误**：{error}"},
            {"tag": "markdown",
             "content": "请检查 base_url、API key 以及 /v1/models 端点是否可用。"},
            {"tag": "markdown", "content": _CARD_SIGNATURE},
        ],
    }


def _build_provider_card(provider_slug: str, models: List[str]) -> Dict[str, Any]:
    """Build a Feishu interactive card with model buttons (5 per row)."""
    cur_model, cur_provider = _current_model_and_provider()
    active_model = _current_model_for_provider(provider_slug)
    emoji = _provider_emoji(provider_slug.split(":")[-1])
    label = provider_slug.split(":")[-1]
    count = len(models)

    elements: List[Dict[str, Any]] = []

    # --- Status line ---
    status_parts: List[str] = []
    if cur_model:
        marker = " ✅" if cur_provider == provider_slug else ""
        status_parts.append(f"当前：`{cur_model}` ({cur_provider}){marker}")
    status_parts.append(f"共 **{count}** 个模型")
    elements.append({
        "tag": "markdown",
        "content": " · ".join(status_parts),
    })

    # --- Hint ---
    elements.append({
        "tag": "markdown",
        "content": f"👇 选择 **{emoji} {label}** 模型：",
    })

    # --- Model buttons or error ---
    if not models:
        elements.append({
            "tag": "markdown",
            "content": (
                f"⚠️ 未在 config.yaml 中找到 `{provider_slug}` 的模型列表。\n"
                f"_检查 custom_providers[] 是否定义了 name/provider 和 models。_"
            ),
        })
    else:
        for i in range(0, len(models), _BUTTONS_PER_ROW):
            row = models[i:i + _BUTTONS_PER_ROW]
            actions: List[Dict[str, Any]] = []
            for m in row:
                text = _display_name(m)
                if active_model and m == active_model:
                    text = f"★ {text}"
                actions.append({
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": text},
                    "type": "default",
                    "value": {
                        "mp_switch": "switch_model",
                        "model": m,
                        "provider": provider_slug,
                    },
                })
            elements.append({"tag": "action", "actions": actions})

    # --- Footer: refresh button + signature ---
    elements.append({
        "tag": "action",
        "actions": [{
            "tag": "button",
            "text": {"tag": "plain_text", "content": "🔄 从供应商刷新模型列表"},
            "type": "primary",
            "value": {
                "mp_switch": "refresh_models",
                "provider": provider_slug,
            },
        }],
    })
    elements.append({
        "tag": "markdown",
        "content": _CARD_SIGNATURE,
    })

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"content": f"🎯 选择 {label} 模型", "tag": "plain_text"},
            "template": "blue",
        },
        "elements": elements,
    }


def _all_custom_providers() -> List[Dict[str, Any]]:
    """Return every provider configured under custom_providers[]."""
    return list(_load_profile_config().get("custom_providers", []) or [])


def _provider_display_name(name: str) -> str:
    """Friendly display name + emoji for a provider's config 'name' field.

    Add your own provider slugs here, or leave the fallback (🔌 + raw name).
    """
    key = (name or "").strip().lower()
    mapping = _PROVIDER_DISPLAY_NAMES
    return mapping.get(key, f"🔌 {name}")


def _provider_uses_model_value(cp: Dict[str, Any]) -> bool:
    """Some providers (e.g. 'agent') expose a single 'model' field rather than
    a models[] dict — there is no separate 'provider' string to route with.
    For those we must switch by setting model to the literal value, not a
    provider/model pair."""
    raw = cp.get("models")
    # agent-style: models is a single None or dict with "None" key
    if isinstance(raw, dict) and list(raw.keys()) == ["None"]:
        return False
    if not raw and cp.get("model"):
        return False
    return False


def _build_all_providers_card_custom() -> Dict[str, Any]:
    """Build a single card grouping models under each provider, using the
    provider's name as the switch slug. Every provider from
    custom_providers[] appears, grouped, with all its models as buttons.
    The button value uses the resolved provider slug so switch_model() can
    resolve it directly.
    """
    cur_model, cur_provider = _current_model_and_provider()
    providers = _all_custom_providers()

    elements: List[Dict[str, Any]] = [
        {
            "tag": "markdown",
            "content": (
                f"**Hermes 当前模型**：`{cur_model or '—'}` （`{cur_provider or '—'}`）\n\n"
                "★ = 该 provider 当前正在使用的模型\n\n"
                "👇 点击任意模型即可切换："
            ),
        },
        {"tag": "hr"},
    ]

    if not providers:
        elements.append({
            "tag": "markdown",
            "content": "⚠️ 未在 config.yaml 找到任何 `custom_providers[]` 配置。",
        })
    else:
        for cp in providers:
            name = cp.get("name") or cp.get("provider") or "unknown"
            slug = str(name).strip()
            # Resolve the slug the same way the switch path expects.
            provider_for_switch = _resolve_provider_slug(slug)
            label = _provider_display_name(name)
            models = _provider_models(cp)
            active_model = _current_model_for_provider(provider_for_switch)
            if not models:
                elements.append({"tag": "hr"})
                elements.append({
                    "tag": "markdown",
                    "content": f"**{label}** — _无可用模型_",
                })
                continue
            elements.append({"tag": "hr"})
            heading = f"**{label}** · 共 {len(models)} 个模型"
            if active_model:
                heading += f" · 当前 ★ `{active_model}`"
            elements.append({
                "tag": "markdown",
                "content": heading,
            })
            for i in range(0, len(models), _BUTTONS_PER_ROW):
                row = models[i:i + _BUTTONS_PER_ROW]
                actions: List[Dict[str, Any]] = []
                for m in row:
                    text = _display_name(m)
                    if active_model and m == active_model:
                        text = f"★ {text}"
                    actions.append({
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": text},
                        "type": "default",
                        "value": {
                            "mp_switch": "switch_model",
                            "model": m,
                            "provider": provider_for_switch,
                        },
                    })
                elements.append({"tag": "action", "actions": actions})
            elements.append({
                "tag": "action",
                "actions": [{
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": f"🔄 刷新{label}"},
                    "type": "primary",
                    "value": {
                        "mp_switch": "refresh_models",
                        "provider": provider_for_switch,
                    },
                }],
            })

    elements.append({"tag": "hr"})
    elements.append({"tag": "markdown", "content": _CARD_SIGNATURE})

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"content": "🎛️ 更换模型", "tag": "plain_text"},
            "template": "blue",
        },
        "elements": elements,
    }


def _provider_models(cp: Dict[str, Any]) -> List[str]:
    """Extract the model id list from a custom_providers[] entry.

    Handles: 'models' dict, 'models' list (str or {id}), and a single
    top-level 'model' field (e.g. agent provider with no models dict).
    """
    out: List[str] = []
    raw = cp.get("models")
    if isinstance(raw, dict):
        for m in raw.keys():
            if isinstance(m, str) and m and m not in out:
                out.append(m)
    elif isinstance(raw, list):
        for m in raw:
            if isinstance(m, str) and m and m not in out:
                out.append(m)
            elif isinstance(m, dict):
                mid = m.get("id")
                if isinstance(mid, str) and mid and mid not in out:
                    out.append(mid)
    if not out:
        single = cp.get("model")
        if isinstance(single, str) and single.strip():
            out.append(single.strip())
    return out


def _build_ack_card(model_id: str, provider_slug: str, ok: bool, detail: str = "") -> Dict[str, Any]:
    emoji = _provider_emoji(provider_slug.split(":")[-1])
    label = _display_name(model_id)
    if ok:
        body = (
            f"**模型**：{emoji} {label}\n"
            f"**Provider**：`{provider_slug}`\n\n"
            "✨ 下一次消息即用新模型。"
        )
        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"content": "✅ 已切换", "tag": "plain_text"},
                "template": "green",
            },
            "elements": [
                {"tag": "markdown", "content": body},
                {"tag": "markdown", "content": _CARD_SIGNATURE},
            ],
        }
    target_line = f"**目标**：{label} ({provider_slug})"
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"content": "❌ 切换失败", "tag": "plain_text"},
            "template": "red",
        },
        "elements": [
            {"tag": "markdown", "content": target_line},
            {"tag": "markdown", "content": f"```\n{(detail or 'unknown error')[:500]}\n```"},
            {"tag": "markdown", "content": _CARD_SIGNATURE},
        ],
    }


# ---------------------------------------------------------------------------
# Card sending
# ---------------------------------------------------------------------------


def _send_feishu_card(adapter: Any, chat_id: str, card: Dict[str, Any]) -> Optional[str]:
    """Send an interactive card to a Feishu chat using the adapter.

    The pre_gateway_dispatch hook runs on the gateway's event loop. Calling
    run_coroutine_threadsafe(...).result() from inside that loop deadlocks
    (the loop is busy running our hook and can't make progress). And
    ensure_future is unreliable here because the loop may be in a transient
    non-running state during the hook.

    Strategy: spawn a short-lived daemon thread with its own event loop that
    runs the send coroutine to completion. This sidesteps all loop/threading
    ambiguity. The hook returns immediately (fire-and-forget); the thread
    logs the outcome via _log_send_result.
    """
    send_fn = getattr(adapter, "_feishu_send_with_retry", None)
    if send_fn is None:
        logger.warning(
            "[ModelPicker] No _feishu_send_with_retry on adapter; "
            "adapter type=%s", type(adapter).__name__,
        )
        return None

    loop = getattr(adapter, "_loop", None)
    payload = json.dumps(card, ensure_ascii=False)
    logger.info(
        "[ModelPicker] _send_feishu_card: chat=%s payload_bytes=%d "
        "loop=%s loop_running=%s",
        chat_id, len(payload),
        "found" if loop else "None",
        loop.is_running() if loop else "n/a",
    )

    def _worker():
        # Run the send coroutine in a fresh event loop inside this thread.
        # We never touch the gateway's main loop, so there's no deadlock
        # or cross-thread scheduling issue.
        fresh_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(fresh_loop)
        try:
            coro = send_fn(
                chat_id=chat_id,
                msg_type="interactive",
                payload=payload,
                reply_to=None,
                metadata=None,
            )
            response = fresh_loop.run_until_complete(coro)
            # Log response code/msg — this tells us whether the API actually
            # succeeded. response.code == 0 means success on Feishu.
            resp_code = getattr(response, "code", "no_code")
            resp_msg = getattr(response, "msg", "") or ""
            resp_success = None
            try:
                s = getattr(response, "success", None)
                resp_success = s() if callable(s) else s
            except Exception:
                pass
            resp_data = getattr(response, "data", None)
            resp_data_id = None
            if resp_data is not None:
                resp_data_id = getattr(resp_data, "message_id", None)
            if resp_code == 0:
                logger.info(
                    "[ModelPicker] Card send OK chat=%s: data.message_id=%s",
                    chat_id, resp_data_id,
                )
            else:
                logger.warning(
                    "[ModelPicker] Card send API-ERROR chat=%s: code=%s msg=%s "
                    "success=%s",
                    chat_id, resp_code, resp_msg, resp_success,
                )
        except Exception as e:
            import traceback as _tb
            logger.warning(
                "[ModelPicker] Card send FAILED for chat=%s: %s: %s\n%s",
                chat_id, type(e).__name__, e,
                _tb.format_exc(),
            )
        finally:
            try:
                fresh_loop.close()
            except Exception:
                pass
            asyncio.set_event_loop(None)

    try:
        t = threading.Thread(
            target=_worker,
            name="feishu-model-picker-send",
            daemon=True,
        )
        t.start()
    except Exception as e:
        import traceback as _tb
        logger.warning(
            "[ModelPicker] Failed to start send thread: %s\n%s",
            e, _tb.format_exc(),
        )
        return None
    return "async"


def _log_send_result(fut: "asyncio.Future[Any]") -> None:
    """Legacy callback — kept for any existing references."""
    try:
        exc = fut.exception()
        if exc is not None:
            logger.warning(
                "[ModelPicker] Async card send failed: %s: %s",
                type(exc).__name__, exc,
            )
    except Exception as e:
        logger.warning("[ModelPicker] _log_send_result error: %s", e)


# ---------------------------------------------------------------------------
# Confirmation card for system commands
# ---------------------------------------------------------------------------

_SYSTEM_CMD_EMOJI: Dict[str, str] = {
    "stop": "🛑",
    "compress": "🗜",
    "usage": "📊",
    "status": "ℹ️",
    "retry": "🔄",
    "new": "🆕",
    "clear": "🧹",
    "history": "📜",
    "help": "❓",
}


def _build_system_cmd_card(original_text: str, mapped_cmd: str) -> Dict[str, Any]:
    """Build a simple confirmation card for system commands."""
    cmd_short = mapped_cmd.lstrip("/").split()[0] if mapped_cmd.startswith("/") else mapped_cmd
    emoji = _SYSTEM_CMD_EMOJI.get(cmd_short, "⚙️")

    elements: List[Dict[str, Any]] = [
        {
            "tag": "markdown",
            "content": (
                f"**指令**：`{original_text}`\n"
                f"**命令**：`{mapped_cmd}`\n\n"
                "⏳ 处理中，请稍候..."
            ),
        },
        {
            "tag": "markdown",
            "content": _CARD_SIGNATURE,
        },
    ]

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"content": f"{emoji} 正在执行：{cmd_short}", "tag": "plain_text"},
            "template": "blue",
        },
        "elements": elements,
    }


# ---------------------------------------------------------------------------
# Actual model switch (runs on gateway loop)
# ---------------------------------------------------------------------------


async def _do_switch_and_ack(*, adapter: Any, chat_id: str,
                             model_id: str, provider_slug: str,
                             gateway: Any) -> None:
    """Switch the active Hermes model, then acknowledge with a card."""
    ok = False
    detail = ""
    try:
        from hermes_cli.model_switch import switch_model
        from hermes_cli.config import get_compatible_custom_providers
        cfg = _load_profile_config()
        model_blk = cfg.get("model", {}) or {}
        cur_model = str(model_blk.get("default", "") or "")
        cur_provider = str(model_blk.get("provider", "") or "")
        cur_base_url = str(model_blk.get("base_url", "") or "")
        cur_api_key = str(model_blk.get("api_key", "") or "")

        result = await asyncio.to_thread(
            switch_model,
            raw_input=model_id,
            current_provider=cur_provider,
            current_model=cur_model,
            current_base_url=cur_base_url,
            current_api_key=cur_api_key,
            is_global=True,
            explicit_provider=provider_slug,
            user_providers=cfg.get("providers"),
            custom_providers=get_compatible_custom_providers(cfg)
                if hasattr(get_compatible_custom_providers, "__call__")
                else cfg.get("custom_providers"),
        )
        if not getattr(result, "success", False):
            ok = False
            detail = str(getattr(result, "error_message", "") or "switch_model failed")
        else:
            ok = True
            # switch_model() only resolves; the gateway's /model handler persists
            # separately via save_config(). Mirror that logic here so the
            # switch actually survives across sessions (not just this process).
            try:
                from hermes_cli.config import read_user_config_raw, save_config
                cfg_path = _profile_config_path()
                persist_cfg = read_user_config_raw(str(cfg_path))
                raw_model = persist_cfg.get("model")
                if isinstance(raw_model, dict):
                    model_blk = raw_model
                elif isinstance(raw_model, str) and raw_model.strip():
                    model_blk = {"default": raw_model.strip()}
                    persist_cfg["model"] = model_blk
                else:
                    model_blk = {}
                    persist_cfg["model"] = model_blk
                model_blk["default"] = result.new_model
                model_blk["provider"] = result.target_provider
                # Custom providers have no registry entry to re-derive from;
                # clear stale base_url when resolver returned empty.
                is_custom = str(result.target_provider or "").strip().lower() == "custom"
                if result.base_url:
                    model_blk["base_url"] = result.base_url
                elif is_custom:
                    model_blk.pop("base_url", None)
                save_config(persist_cfg)
                logger.info("[ModelPicker] Persisted to config: %s / %s", result.new_model, result.target_provider)
            except Exception as exc:
                logger.warning("[ModelPicker] Persist to config failed (in-memory switch still active): %s", exc)

            # Best-effort: evict cached agent so the next turn rebuilds from the
            # updated config.yaml. Without this, the current session's cached
            # agent keeps using the OLD model until idle eviction.
            try:
                _runner = getattr(gateway, "_runner", None) or gateway
                # Derive session_key the same way gateway does for feishu dm.
                source = getattr(gateway, "session_store", None)
                chat_id_local = chat_id
                # Build session key matching gateway convention:
                # "agent:<profile>:feishu:dm:<chat_id>"
                profile = "main"
                session_key = f"agent:{profile}:feishu:dm:{chat_id_local}"
                _evict = getattr(_runner, "_evict_cached_agent", None)
                if _evict is not None:
                    _evict(session_key)
                    logger.debug("[ModelPicker] Evicted cached agent for session %s", session_key)
            except Exception as ex:
                logger.debug("[ModelPicker] Cache evict skipped: %s", ex)
    except Exception as e:
        ok = False
        detail = str(e)

    ack_card = _build_ack_card(model_id, provider_slug, ok, detail)
    _send_feishu_card(adapter, chat_id, ack_card)
    logger.info(
        "[ModelPicker] Model switch %s -> %s on %s (ok=%s)",
        _current_model_and_provider()[0], model_id, provider_slug, ok,
    )


async def _do_refresh_and_resent(*, adapter: Any, chat_id: str,
                                 provider_slug: str) -> None:
    """Fetch live models from the provider, persist, and re-send the picker card."""
    ok, result = await asyncio.to_thread(_fetch_provider_models, provider_slug)
    if not ok:
        _send_feishu_card(adapter, chat_id, _build_refresh_error_card(provider_slug, result))
        logger.warning("[ModelPicker] Refresh failed for %s: %s", provider_slug, result)
        return

    ids = list(result)  # type: ignore[arg-type]
    persisted = _persist_provider_models(provider_slug, ids)

    # Build a fresh provider card with the new model list.
    card = _build_provider_card(provider_slug, ids)
    # Add a status banner on top so the user sees refresh result.
    status_text = (
        f"✅ 已从供应商刷新，共 **{len(ids)}** 个模型"
        + ("（已写入 config.yaml）" if persisted else "（未写入 config.yaml）")
    )
    banner = {"tag": "markdown", "content": status_text}
    existing_elements = card.get("elements", []) or []
    card["elements"] = [banner] + existing_elements

    _send_feishu_card(adapter, chat_id, card)
    logger.info(
        "[ModelPicker] Refreshed models for %s: %d new ids (persisted=%s)",
        provider_slug, len(ids), persisted,
    )


# ---------------------------------------------------------------------------
# Hook entry
# ---------------------------------------------------------------------------


def _clean_menu_text(text: str) -> str:
    """Clean a menu item text for alias matching.

    Strips leading/trailing whitespace, removes emoji characters, and
    lowercases. Used to match Chinese aliases like '停止操作' or 'myprovider'.
    """
    cleaned = text.strip()
    # Remove common emoji ranges (simplified: strip non-alphanumeric non-Chinese)
    # Keep Chinese characters and alphanumeric.
    result = []
    for ch in cleaned:
        # Keep Chinese (CJK) and alphanumeric
        if '\u4e00' <= ch <= '\u9fff' or ch.isalnum() or ch in ('_', '-', ':'):
            result.append(ch)
    return ''.join(result).lower()


def _pre_gateway_dispatch(event: Any, gateway: Any, **kwargs) -> Optional[Dict[str, Any]]:
    """Intercept /model pick <provider>, /card switch_model, and Chinese aliases."""
    try:
        text = getattr(event, "text", "") or ""
    except Exception:
        return None


    if not isinstance(text, str):
        return None

    # Extract source and chat_id early (needed for alias logging too)
    source = getattr(event, "source", None)
    if source is None or not _is_feishu(source):
        return None

    chat_id = getattr(source, "chat_id", "") or ""
    if not chat_id:
        return None

    # Chinese alias resolution: strip emoji/whitespace, match _CHINESE_ALIASES
    cleaned = _clean_menu_text(text)
    logger.debug(
        "[ModelPicker] Processing text=%r cleaned=%r is_chinese_alias=%s",
        text, cleaned, cleaned in _CHINESE_ALIASES,
    )
    if cleaned in _CHINESE_ALIASES:
        mapped_cmd = _CHINESE_ALIASES[cleaned]
        logger.info(
            "[ModelPicker] Chinese alias '%s' -> '%s' (chat=%s)",
            text.strip(), mapped_cmd, chat_id,
        )
        # Resolve adapter for card sending
        try:
            runner = getattr(gateway, "_runner", None) or gateway
            adapter = runner._adapter_for_source(source) if runner is not None else None
        except Exception as e:
            logger.warning("[ModelPicker] _adapter_for_source failed: %s", e)
            adapter = None

        # --- /mp alias: send confirmation card, then send model picker card, skip ---
        if mapped_cmd.startswith("/mp"):
            if adapter is not None:
                card = _build_system_cmd_card(text.strip(), mapped_cmd)
                _send_feishu_card(adapter, chat_id, card)
                logger.info(
                    "[ModelPicker] Sent system cmd card for '%s' to %s",
                    mapped_cmd, chat_id,
                )
            # Send the actual model picker card (same as /mp <provider>)
            if adapter is not None:
                m = _TRIGGER_RE.match(mapped_cmd.strip())
                if m:
                    raw_slug = m.group(1)
                    provider_slug = _resolve_provider_slug(raw_slug)
                    models = _list_models_for_provider(provider_slug)
                    picker_card = _build_provider_card(provider_slug, models)
                    msg_id = _send_feishu_card(adapter, chat_id, picker_card)
                    if msg_id:
                        logger.info(
                            "[ModelPicker] Sent provider card for %s (%d models) to %s",
                            provider_slug, len(models), chat_id,
                        )
                    else:
                        logger.warning(
                            "[ModelPicker] Card send failed for %s to %s",
                            provider_slug, chat_id,
                        )
            return {"action": "skip", "reason": f"model-picker card via alias for {mapped_cmd}"}

        # --- System command alias: send confirmation card, then rewrite for gateway ---
        if adapter is not None:
            card = _build_system_cmd_card(text.strip(), mapped_cmd)
            _send_feishu_card(adapter, chat_id, card)
            logger.info(
                "[ModelPicker] Sent system cmd card for '%s' to %s",
                mapped_cmd, chat_id,
            )
        return {"action": "rewrite", "text": mapped_cmd}

    if not text.startswith("/"):
        # Chinese free-text trigger "更换模型"/"切换模型"/"换模型" (no slash)
        # → resolve adapter and send the ALL-providers picker card.
        if _ALL_TRIGGER_RE.match(text.strip()):
            try:
                runner = getattr(gateway, "_runner", None) or gateway
                adapter = runner._adapter_for_source(source) if runner is not None else None
            except Exception as e:
                logger.warning("[ModelPicker] _adapter_for_source failed: %s", e)
                adapter = None
            if adapter is None:
                return None
            card = _build_all_providers_card_custom()
            msg_id = _send_feishu_card(adapter, chat_id, card)
            if msg_id:
                logger.info("[ModelPicker] Sent all-providers picker card to %s", chat_id)
            else:
                logger.warning("[ModelPicker] All-providers card send failed to %s", chat_id)
            return {"action": "skip", "reason": "all-providers model picker card"}
        return None

    # Resolve adapter for card sending
    adapter = None
    try:
        runner = getattr(gateway, "_runner", None) or gateway
        adapter = runner._adapter_for_source(source) if runner is not None else None
    except Exception as e:
        logger.warning("[ModelPicker] _adapter_for_source failed: %s", e)
    if adapter is None:
        return None

    # --- Case 0: bare "/model" / "/models" / "/model pick" (slash form) → ALL providers card ---
    if _ALL_TRIGGER_RE.match(text.strip()):
        card = _build_all_providers_card_custom()
        msg_id = _send_feishu_card(adapter, chat_id, card)
        if msg_id:
            logger.info("[ModelPicker] Sent all-providers picker card to %s", chat_id)
        else:
            logger.warning("[ModelPicker] All-providers card send failed to %s", chat_id)
        return {"action": "skip", "reason": "all-providers model picker card"}

    # --- Case 1: /model pick <provider> ---
    m = _TRIGGER_RE.match(text.strip())
    if m:
        raw_slug = m.group(1)
        provider_slug = _resolve_provider_slug(raw_slug)
        models = _list_models_for_provider(provider_slug)
        card = _build_provider_card(provider_slug, models)
        msg_id = _send_feishu_card(adapter, chat_id, card)
        if msg_id:
            logger.info(
                "[ModelPicker] Sent provider card for %s (%d models) to %s",
                provider_slug, len(models), chat_id,
            )
        else:
            # Card send failed — still skip, don't fall through to built-in
            # /model command. Fall-through would give a bogus error message
            # like "Model names cannot contain spaces" instead of our card.
            logger.warning(
                "[ModelPicker] Card send failed for %s to %s; skipping anyway "
                "to avoid built-in /model handling.",
                provider_slug, chat_id,
            )
        return {"action": "skip", "reason": f"model-picker card for {provider_slug}"}

    # --- Case 1b: /model refresh <provider> ---
    m = _REFRESH_RE.match(text.strip())
    if m:
        raw_slug = m.group(1)
        provider_slug = _resolve_provider_slug(raw_slug)
        # Fetch live model list from the provider's /v1/models endpoint.
        ok, result = _fetch_provider_models(provider_slug)
        if ok:
            ids = result  # type: ignore[assignment]
            persisted = _persist_provider_models(provider_slug, list(ids))
            # Rebuild the provider card with the fresh model list.
            card = _build_provider_card(provider_slug, list(ids))
            # Attach a status footer via a custom element so the user sees
            # whether the discovery actually landed in config.yaml.
            status_text = (
                f"✅ 已从供应商刷新，共 {len(ids)} 个模型"
                + ("（已写入 config.yaml）" if persisted else "（未写入 config.yaml）")
            )
            # The card builder already returns a full card dict; we append an
            # element by walking the markdown block, but to keep it simple we
            # just send a separate status text via adapter.send_message.
            _send_feishu_card(adapter, chat_id, card)
            try:
                loop = getattr(adapter, "_loop", None)
                if loop is None or loop.is_closed():
                    loop = asyncio.get_event_loop()
                send_fn = getattr(adapter, "_feishu_send_with_retry", None) \
                    or getattr(adapter, "send_message", None)
                if send_fn is not None:
                    f = asyncio.run_coroutine_threadsafe(
                        send_fn(chat_id=chat_id, text=status_text),
                        loop,
                    )
                    f.result(timeout=10)
            except Exception as e:
                logger.warning("[ModelPicker] Status text send failed: %s", e)
            logger.info(
                "[ModelPicker] Refreshed models for %s: %d new ids",
                provider_slug, len(ids),
            )
            return {"action": "skip", "reason": f"model-picker refresh for {provider_slug}"}
        else:
            # Send an error card back so the user knows what failed.
            err_card = {
                "config": {"wide_screen_mode": True},
                "header": {
                    "title": {"tag": "plain_text", "content": f"❌ 刷新 {provider_slug} 失败"},
                    "template": "red",
                },
                "elements": [
                    {"tag": "markdown",
                     "content": f"**错误**：{result}"},
                    {"tag": "markdown",
                     "content": f"提供商 slug: `{provider_slug}`\n"
                                f"请检查 base_url、API key、以及 /v1/models 端点是否可用。"},
                ],
            }
            _send_feishu_card(adapter, chat_id, err_card)
            logger.warning("[ModelPicker] Refresh failed for %s: %s", provider_slug, result)
            return {"action": "skip", "reason": f"model-picker refresh error for {provider_slug}"}

    # --- Case 2: /card <tag> <json> with mp_switch=switch_model or refresh_models ---
    m = _CARD_RE.match(text.strip())
    if m:
        try:
            payload = json.loads(m.group(2))
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        action_kind = payload.get("mp_switch")
        if action_kind not in ("switch_model", "refresh_models"):
            return None
        provider_slug = payload.get("provider") or ""

        if action_kind == "refresh_models":
            # Refresh: fetch live model list from the provider and re-send the card.
            if not provider_slug:
                logger.warning("[ModelPicker] Malformed refresh payload: %r", payload)
                return None
            logger.info(
                "[ModelPicker] Refreshing models for %s (chat %s)",
                provider_slug, chat_id,
            )
            # Schedule on the gateway loop so we don't block the adapter
            # thread during the HTTP call.
            loop = getattr(adapter, "_loop", None)
            if loop is None or loop.is_closed():
                try:
                    loop = asyncio.get_event_loop()
                except Exception:
                    return None
            try:
                asyncio.run_coroutine_threadsafe(
                    _do_refresh_and_resent(
                        adapter=adapter,
                        chat_id=chat_id,
                        provider_slug=provider_slug,
                    ),
                    loop,
                )
                return {"action": "skip", "reason": "model-picker refresh scheduled"}
            except Exception as e:
                logger.warning("[ModelPicker] Failed to schedule refresh: %s", e)
                return None

        if not provider_slug:
            logger.warning("[ModelPicker] Malformed switch_model payload: %r", payload)
            return None
        model_id = payload.get("model") or ""
        if not model_id:
            logger.warning("[ModelPicker] Missing model in switch_model payload: %r", payload)
            return None

        logger.info(
            "[ModelPicker] Switching -> %s on %s (chat %s)",
            model_id, provider_slug, chat_id,
        )

        # Schedule the switch on the gateway's loop so we can call switch_model()
        # and adapter.send concurrently with the gateway.
        loop = getattr(adapter, "_loop", None)
        if loop is None or loop.is_closed():
            try:
                loop = asyncio.get_event_loop()
            except Exception:
                return None
        try:
            asyncio.run_coroutine_threadsafe(
                _do_switch_and_ack(
                    adapter=adapter,
                    chat_id=chat_id,
                    model_id=model_id,
                    provider_slug=provider_slug,
                    gateway=gateway,
                ),
                loop,
            )
            return {"action": "skip", "reason": "model-picker switch scheduled"}
        except Exception as e:
            logger.warning("[ModelPicker] Failed to schedule switch: %s", e)
            return None

    return None


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------


def register(ctx: Any) -> None:
    """Register pre_gateway_dispatch hook. Called by Hermes plugin loader."""
    try:
        ctx.register_hook("pre_gateway_dispatch", _pre_gateway_dispatch)
        logger.info("[ModelPicker] Registered pre_gateway_dispatch hook")
    except Exception as e:
        logger.warning("[ModelPicker] Failed to register hook: %s", e)
