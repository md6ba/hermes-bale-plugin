"""Bale adapter — a thin subclass of the Telegram adapter.

Bale's Bot API (docs.bale.ai) is Telegram's Bot API with minor changes, so we
reuse Hermes' full Telegram adapter and override only what differs:

  * API base URL  -> https://tapi.bale.ai/bot   (set via PlatformConfig.extra.base_url)
  * Bot token     -> BALE_BOT_TOKEN             (instead of TELEGRAM_BOT_TOKEN)
  * Allowed users -> BALE_ALLOWED_USERS         (falls back to TELEGRAM_ALLOWED_USERS)

All slash commands, the command list, session binding, inline keyboards, media,
voice, and delivery behavior are inherited unchanged from TelegramAdapter — the
Bale experience is therefore exactly the Telegram experience.

NOTE on the base URL: python-telegram-bot builds the final endpoint as
``base_url + token`` (NOT ``base_url + "bot" + token``), so the Bale base URL
MUST end in ``/bot`` — i.e. ``https://tapi.bale.ai/bot``. That yields
``https://tapi.bale.ai/bot<token>`` which is exactly what Bale's Bot API expects
(the documented endpoint is ``https://tapi.bale.ai/bot<token>/getMe``).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Re-use the real Telegram adapter implementation.
from plugins.platforms.telegram.adapter import (  # noqa: E402
    TelegramAdapter,
    check_telegram_requirements,
    Platform,
    PlatformConfig,
)
from gateway.platform_registry import (  # noqa: E402
    PlatformEntry,
    platform_registry,
)

# IMPORTANT: PTB concatenates base_url + token, so this MUST end in "/bot".
_BALE_API_BASE = "https://tapi.bale.ai/bot"
_BALE_TOKEN_ENV = "BALE_BOT_TOKEN"
_BALE_ALLOWED_ENV = "BALE_ALLOWED_USERS"
_BALE_CHAT_ID_ENV = "BALE_CHAT_ID"


def _bale_token() -> str:
    return os.getenv(_BALE_TOKEN_ENV, "").strip()


def _bale_allowed_users() -> str:
    # Prefer a dedicated Bale allowlist; otherwise reuse the Telegram one,
    # plus the known Bale chat owner id.
    allowed = os.getenv(_BALE_ALLOWED_ENV, "").strip()
    if not allowed:
        allowed = os.getenv("TELEGRAM_ALLOWED_USERS", "").strip()
    chat_id = os.getenv(_BALE_CHAT_ID_ENV, "").strip()
    parts = [p for p in allowed.split(",") if p.strip()]
    if chat_id and chat_id not in parts:
        parts.append(chat_id)
    return ",".join(parts)


class BaleAdapter(TelegramAdapter):
    """Drop-in Bale adapter: same engine as Telegram, different endpoint/token."""

    # Bale message limit matches Telegram's 4096-char cap.
    MAX_MESSAGE_LENGTH = 4096

    def __init__(self, config: PlatformConfig):
        # Inject the Bale token + base_url BEFORE TelegramAdapter.__init__ reads
        # config.token / config.extra.  We mutate the passed config object in
        # place so the parent's connect() (which reads self.config.token and
        # extra["base_url"]) sees Bale values.
        if not getattr(config, "token", None):
            config.token = _bale_token()
        extra = dict(getattr(config, "extra", {}) or {})
        # Force the Bale endpoint. PTB appends the token to base_url, so the
        # value must end in "/bot".
        extra["base_url"] = _BALE_API_BASE
        extra["base_file_url"] = _BALE_API_BASE
        try:
            config.extra = extra
        except AttributeError:
            # PlatformConfig may use a different setter; fall back to __dict__.
            config.__dict__["extra"] = extra
        super().__init__(config)
        # Tell the engine this is Bale (some logs / delivery keys rely on platform).
        try:
            self.platform = Platform("bale")
        except Exception:
            pass

    # Override user authorization so it reads BALE_ALLOWED_USERS.
    def _authorized_user_ids(self):  # type: ignore[override]
        raw = _bale_allowed_users()
        return {u.strip() for u in raw.split(",") if u.strip()}

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        # Parent builds the PTB Application (and the polling loop) here.
        ok = await super().connect(is_reconnect=is_reconnect)
        # Instrument both the raw fetch (get_updates -> Bale's real JSON shape)
        # and the parsed dispatch (process_update) so we can see exactly what
        # Bale sends and whether PTB silently drops it.
        self._bale_instrument_get_updates()
        self._bale_instrument_process_update()
        return ok

    def _bale_instrument_get_updates(self) -> None:
        """Wrap bot.get_updates to log Bale's RAW update JSON on the first hit.

        PTB parses each update via Update.de_json *before* process_update runs.
        If Bale's JSON diverges (non-standard nesting / types), de_json can yield
        an Update with no `message` — silently dropped, invisible to process_update.
        Logging the raw list here captures the exact shape for a one-shot fix.
        """
        import types
        bot = getattr(self, "_bot", None) or getattr(self, "_app", None)
        if bot is None:
            return
        # Capture the ORIGINAL unbound function (NOT the bound method) so calling
        # it as real_fn(self, *a, **k) is recursion-free and passes offset/timeout
        # exactly as PTB intended. Using the bound method here caused infinite
        # recursion + "got multiple values for argument 'offset'".
        real_fn = type(bot).get_updates
        _logged = {"done": False}

        async def _wrapped_get(self, *a, **k):
            result = await real_fn(self, *a, **k)
            if result and not _logged["done"]:
                _logged["done"] = True  # log only the first non-empty poll
                try:
                    raw_list = [r.to_dict() if hasattr(r, "to_dict") else r for r in result]
                    logger.warning(
                        "[Bale] RAW getUpdates returned %d update(s): %s",
                        len(raw_list),
                        json.dumps(raw_list, ensure_ascii=False)[:3000],
                    )
                except Exception:
                    pass
            return result

        try:
            bot.get_updates = types.MethodType(_wrapped_get, bot)
        except Exception:
            try:
                type(bot).get_updates = _wrapped_get
            except Exception:
                pass

    def _bale_instrument_process_update(self) -> None:
        """Wrap Application.process_update to log raw Bale updates + parse errors.

        Bale's Bot API is Telegram-compatible *in theory*, but real updates may
        carry field/type quirks that make PTB's Update.de_json produce an Update
        with no `message` (silently dropped). Logging the first raw update + any
        exception makes the divergence visible without requiring a human tester.
        """
        app = getattr(self, "_app", None)
        if app is None:
            return
        real_process = app.process_update

        async def _wrapped(update, *a, **k):
            # update is already a telegram.Update here; grab the raw dict if present
            raw = getattr(update, "to_dict", lambda: None)()
            if raw is not None:
                logger.warning(
                    "[Bale] PARSED inbound update #%s: %s",
                    getattr(update, "update_id", "?"),
                    json.dumps(raw, ensure_ascii=False)[:2000],
                )
            try:
                return await real_process(update, *a, **k)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "[Bale] process_update raised on update #%s: %s: %s",
                    getattr(update, "update_id", "?"),
                    type(exc).__name__,
                    exc,
                )
                raise

        try:
            app.process_update = _wrapped
        except Exception:
            # ExtBot/Application may guard attribute assignment; fall back to
            # patching the bound method via the class (affects only this instance).
            try:
                type(app).process_update = _wrapped
            except Exception:
                pass



def _build_adapter(config):
    adapter = BaleAdapter(config)
    try:
        adapter._notifications_mode = _resolve_notifications_mode()
    except Exception:
        adapter._notifications_mode = "important"
    return adapter


def _resolve_notifications_mode() -> str:
    try:
        import hermes_cli.gateway as gateway_mod
        return getattr(gateway_mod, "get_notifications_mode", lambda: "important")()
    except Exception:
        return "important"


def _is_connected(config) -> bool:
    token = getattr(config, "token", None)
    if not token:
        token = _bale_token()
    return bool(str(token).strip())


def _apply_yaml_config(yaml_cfg: dict, bale_cfg: dict) -> dict | None:
    """Inject Bale-specific extras (base_url) into PlatformConfig.extra.

    Mirrors the telegram apply_yaml_config_fn contract: called from
    load_gateway_config() BEFORE the adapter is constructed, so the adapter
    sees base_url in config.extra.
    """
    extras: dict = {}
    extras["base_url"] = _BALE_API_BASE
    extras["base_file_url"] = _BALE_API_BASE
    tok = _bale_token()
    if tok:
        extras["_bale_token"] = tok
    allowed = _bale_allowed_users()
    if allowed:
        extras["_bale_allowed"] = allowed
    return extras or None


def register(ctx) -> None:
    """Plugin entry point — called by the Hermes plugin system."""
    ctx.register_platform(
        name="bale",
        label="Bale",
        adapter_factory=_build_adapter,
        check_fn=check_telegram_requirements,
        is_connected=_is_connected,
        required_env=[_BALE_TOKEN_ENV],
        install_hint="Set BALE_BOT_TOKEN in ~/.hermes/.env (and BALE_ALLOWED_USERS).",
        allowed_users_env=_BALE_ALLOWED_ENV,
        allow_all_env="BALE_ALLOW_ALL_USERS",
        cron_deliver_env_var="BALE_HOME_CHANNEL",
        max_message_length=4096,
        emoji="📦",
        allow_update_command=True,
        plugin_name="bale-platform",
        apply_yaml_config_fn=_apply_yaml_config,
    )
