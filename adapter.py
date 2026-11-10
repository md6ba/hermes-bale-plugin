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
from gateway.platforms.send_targets import StandaloneSendPolicy  # noqa: E402

# IMPORTANT: PTB concatenates base_url + token, so this MUST end in "/bot".
_BALE_API_BASE = "https://tapi.bale.ai/bot"
_BALE_TOKEN_ENV = "BALE_BOT_TOKEN"
_BALE_ALLOWED_ENV = "BALE_ALLOWED_USERS"
_BALE_CHAT_ID_ENV = "BALE_CHAT_ID"


def _bale_token() -> str:
    return os.getenv(_BALE_TOKEN_ENV, "").strip()


def _bale_chat_id() -> str:
    """Owner chat id used as the delivery target when a cron job targets `bale` root.
    BALE_CHAT_ID wins; BALE_HOME_CHANNEL is the usual home-channel var and is the fallback."""
    return os.getenv(_BALE_CHAT_ID_ENV, "").strip() or os.getenv("BALE_HOME_CHANNEL", "").strip()


_BALE_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
_BALE_VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".3gp"}
_BALE_VOICE_EXTS = {".ogg", ".opus"}
_BALE_AUDIO_EXTS = {".mp3", ".m4a", ".m2a", ".wav", ".flac"}


def _bale_media_endpoint(path, is_voice, force_document=False):
    """Pick the Bale Bot API method + multipart file field for one media file.

    Bale follows the Telegram Bot API method names (sendPhoto / sendVideo /
    sendAudio / sendVoice / sendDocument) and takes the file under a field
    named after the method ("photo", "document", ...)."""
    if force_document:
        return "sendDocument", "document"
    ext = os.path.splitext(path)[1].lower()
    if is_voice or ext in _BALE_VOICE_EXTS:
        return "sendVoice", "voice"
    if ext in _BALE_IMAGE_EXTS:
        return "sendPhoto", "photo"
    if ext in _BALE_VIDEO_EXTS:
        return "sendVideo", "video"
    if ext in _BALE_AUDIO_EXTS:
        return "sendAudio", "audio"
    return "sendDocument", "document"


def _bale_post_multipart(token, method, fields, file_field, file_path):
    """multipart/form-data upload of one file to a Bale Bot API method (stdlib only)."""
    import mimetypes
    import urllib.request
    import uuid

    boundary = f"----hermes-bale-{uuid.uuid4().hex}"
    parts = []
    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode("utf-8")
        )
    filename = os.path.basename(file_path)
    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    with open(file_path, "rb") as fh:
        data = fh.read()
    parts.append(
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
            f"Content-Type: {mime}\r\n\r\n"
        ).encode("utf-8")
        + data
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    req = urllib.request.Request(
        f"{_BALE_API_BASE}{token}/{method}",
        data=b"".join(parts),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8", "replace") or "{}")


def _split_bale_text(text):
    """Bale caps messages at 4096 chars; split on paragraph boundaries where possible."""
    chunks, cur = [], ""
    for para in (text or "").split("\n\n"):
        candidate = f"{cur}\n\n{para}" if cur else para
        if len(candidate) > 4000:
            if cur:
                chunks.append(cur)
            while len(para) > 4000:
                chunks.append(para[:4000])
                para = para[4000:]
            cur = para
        else:
            cur = candidate
    if cur:
        chunks.append(cur)
    return chunks


async def _standalone_send(pconfig, chat_id, message, *, thread_id=None, media_files=None,
                           force_document=False, caption=None):
    """Out-of-process delivery (standalone_sender_fn) so `deliver: bale` cron jobs work without the
    gateway. Bale's Bot API is Telegram-shaped, so we POST to
    ``https://tapi.bale.ai/bot<token>/sendMessage`` directly rather than importing the
    Telegram-only PTB sender (which hardcodes api.telegram.org).

    Media files upload via their type-specific endpoint (sendPhoto/sendVideo/sendAudio/
    sendVoice/sendDocument) as multipart/form-data. When the caller passes a caption
    (single captionable file + short text, per StandaloneSendPolicy.captionable), the text
    rides the first file as its caption instead of a separate message.
    """
    import json as _json
    import urllib.request

    token = getattr(pconfig, "token", None) or _bale_token()
    if not token:
        try:
            from agent.secret_scope import get_secret  # profile-scoped: never borrow another profile's token
            token = get_secret(_BALE_TOKEN_ENV, "") or ""
        except Exception:
            token = ""
    if not token:
        return {"error": "BALE_BOT_TOKEN is not set; cannot deliver to Bale."}

    try:
        chat = int(str(chat_id).strip())
    except (TypeError, ValueError):
        chat = str(chat_id).strip()

    media = list(media_files or [])
    # With a caption the text rides the media bubble; otherwise send it as messages first.
    text = "" if (caption and media) else (message or "")
    chunks = _split_bale_text(text)

    url = f"{_BALE_API_BASE}{token}/sendMessage"
    last_id, warnings = None, []

    for chunk in chunks:
        body = _json.dumps({"chat_id": chat, "text": chunk}).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = _json.loads(resp.read().decode("utf-8", "replace") or "{}")
        except Exception as exc:
            return {"error": f"Bale send failed: {type(exc).__name__}: {exc}"}
        if not payload.get("ok"):
            return {"error": f"Bale API rejected send: {str(payload)[:300]}"}
        last_id = (payload.get("result") or {}).get("message_id")

    for index, entry in enumerate(media):
        path, is_voice = (entry if isinstance(entry, (tuple, list)) else (entry, False))[:2]
        if not path or not os.path.isfile(path):
            return {"error": f"Bale media file not found: {path}"}
        method, field = _bale_media_endpoint(path, is_voice, force_document=force_document)
        fields = {"chat_id": str(chat)}
        if caption and index == 0:
            fields["caption"] = caption[:4096]
        try:
            payload = _bale_post_multipart(token, method, fields, field, path)
        except Exception as exc:
            return {"error": f"Bale media send failed ({method}): {type(exc).__name__}: {exc}"}
        if not payload.get("ok"):
            return {"error": f"Bale API rejected {method}: {str(payload)[:300]}"}
        last_id = (payload.get("result") or {}).get("message_id")

    if not chunks and not media:
        return {"error": "no deliverable text"}

    result = {"success": True, "platform": "bale", "chat_id": str(chat_id), "message_id": str(last_id)}
    if warnings:
        result["warnings"] = warnings
    return result


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
        extra["base_file_url"] = "https://tapi.bale.ai/file/bot"
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

    async def _build_ptb_requests(self) -> tuple:
        """Bale transport: ALWAYS direct — never TELEGRAM_PROXY, never fallback IPs.

        The Telegram parent resolves ``TELEGRAM_PROXY`` (and, with no proxy, a
        Telegram fallback-IP transport that rewrites the API host to Telegram
        datacenter IPs). Both are wrong for Bale: ``tapi.bale.ai`` is an Iranian
        service directly reachable without any proxy, and a proxy hop only adds
        latency/flakiness. Scope-forcing the plain direct branch here so
        Telegram keeps its own proxy route untouched.
        """
        import plugins.platforms.telegram.adapter as _tg_mod

        real_resolve = _tg_mod.resolve_proxy_url
        fallback_env = "HERMES_TELEGRAM_DISABLE_FALLBACK_IPS"
        prev_fallback = os.environ.get(fallback_env)

        def _always_direct(platform_env_var=None, **kw):
            return None

        _tg_mod.resolve_proxy_url = _always_direct
        os.environ[fallback_env] = "1"
        try:
            return await super()._build_ptb_requests()
        finally:
            _tg_mod.resolve_proxy_url = real_resolve
            if prev_fallback is None:
                os.environ.pop(fallback_env, None)
            else:
                os.environ[fallback_env] = prev_fallback

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        # Parent builds the PTB Application (and the polling loop) here.
        ok = await super().connect(is_reconnect=is_reconnect)
        # Instrument both the raw fetch (get_updates -> Bale's real JSON shape)
        # and the parsed dispatch (process_update) so we can see exactly what
        # Bale sends and whether PTB silently drops it.
        self._bale_instrument_get_updates()
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

    # NOTE: an earlier debug wrapper around Application.process_update was removed.
    # It captured `real_process = app.process_update` (a bound method) but assigned
    # `_wrapped` back onto the app; on __slots__-guarded Application instances the
    # instance-assignment fails and the class-level fallback rebinds with wrong
    # arity, so every inbound Bale update died with
    # "TelegramApplication.process_update() takes 2 positional arguments but N were
    # given". Raw Bale payloads are still logged via the get_updates wrapper above.




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
    extras["base_file_url"] = "https://tapi.bale.ai/file/bot"
    tok = _bale_token()
    if tok:
        extras["_bale_token"] = tok
    allowed = _bale_allowed_users()
    if allowed:
        extras["_bale_allowed"] = allowed
    return extras or None


def _parse_target_ref(target_ref: str):
    """Allow ``bale`` / ``bale:home`` as a shorthand for the owner chat so a cron job can target
    the platform root without hardcoding a numeric chat id. Returns None to fall through."""
    ref = (target_ref or "").strip().lower()
    if ref in ("", "home", "me", "self", "default", "owner"):
        chat_id = _bale_chat_id()
        if chat_id:
            return (chat_id, None)
    return None


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
        standalone_sender_fn=_standalone_send,
        # Media sends (documents, photos, ...) route through _standalone_send with
        # multipart uploads to tapi.bale.ai; one captionable file + short text rides
        # as the file's caption. Text keeps using the live adapter in-process.
        standalone_send=StandaloneSendPolicy(label="Bale", media=True, captionable=True,
                                             forward_force_document=True),
        parse_target_ref_fn=_parse_target_ref,
        max_message_length=4096,
        emoji="📦",
        allow_update_command=True,
        plugin_name="bale-platform",
        apply_yaml_config_fn=_apply_yaml_config,
    )
