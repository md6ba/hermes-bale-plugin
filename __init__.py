"""Bale (Iranian messenger) platform plugin for Hermes gateway.

Bale's Bot API is based on Telegram's Bot API (see docs.bale.ai).  We reuse
the battle-tested Telegram adapter directly — subclassing it with only the
three things that differ: API base URL, bot token env var, and allowed-users
env var.  Everything else (slash commands, command list, session binding,
inline keyboards, media, voice, ...) is inherited verbatim from Telegram, so
the Bale experience is identical to Telegram.
"""

from .adapter import register  # noqa: F401
