# hermes-bale-plugin

A **standalone** Bale (Iranian messenger) gateway platform plugin for
[Hermes Agent](https://github.com/NousResearch/hermes-agent).

Bale's Bot API is Telegram-compatible (see docs.bale.ai). This plugin reuses
Hermes' battle-tested **Telegram adapter** verbatim and overrides only the
three things that differ:

| Override        | Value                                            |
|-----------------|--------------------------------------------------|
| API base URL    | `https://tapi.bale.ai/bot` (must end in `/bot`)  |
| Bot token       | `BALE_BOT_TOKEN`                                 |
| Allowed users   | `BALE_ALLOWED_USERS` (falls back to `TELEGRAM_ALLOWED_USERS`) |

Everything else — slash commands, the command list, session binding, inline
keyboards, media, voice, cron/notification delivery — is inherited unchanged
from the Telegram adapter, so the Bale experience is identical to Telegram.

> **Why a standalone plugin and not a PR to upstream?**
> Hermes' contribution guide excludes third-party product integrations from the
> core tree (they impose a maintenance burden on the upstream team). A Bale
> adapter therefore ships as a user plugin you install into `~/.hermes/plugins/`.

---

## Install

```bash
# Clone (or download) this repo:
git clone https://github.com/md6ba/hermes-bale-plugin.git
cd hermes-bale-plugin

# Install into your Hermes user-plugins directory:
mkdir -p ~/.hermes/plugins/bale
cp adapter.py __init__.py plugin.yaml ~/.hermes/plugins/bale/
```

That's it — Hermes auto-discovers plugins under `~/.hermes/plugins/`.

## Configure

Add the Bale bot token to `~/.hermes/.env` (never commit this file):

```ini
# Bale bot token from @BotFather inside the Bale app
BALE_BOT_TOKEN=your-bale-bot-token

# Optional: restrict who can chat with the bot (comma-separated numeric IDs)
BALE_ALLOWED_USERS=1656225902

# Optional: allow every Bale user to chat
# BALE_ALLOW_ALL_USERS=1

# Optional: default Bale chat id for cron/notification delivery
BALE_HOME_CHANNEL=1656225902
```

Enable the plugin in `~/.hermes/config.yaml` (user plugins are opt-in):

```yaml
plugins:
  enabled:
    - bale
```

## Run

Restart the Hermes gateway so it picks up the new platform:

```bash
hermes gateway restart        # or however you run your gateway
```

Verify it connected:

```bash
journalctl --user -u hermes-gateway.service | grep -i bale
```

You should see the platform register and the bot poll `tapi.bale.ai`.

## Notes / pitfalls (learned the hard way)

- **`base_url` MUST end in `/bot`** — python-telegram-bot concatenates
  `base_url + token`, so without the trailing `/bot` you get
  `InvalidURL("Invalid port: '<TOKEN>'")` and the gateway retries forever.
- **Inbound auth gate** reads `TELEGRAM_ALLOWED_USERS` directly inside the
  Telegram adapter's intake, so a Bale-only user (present in `BALE_ALLOWED_USERS`
  but not in `TELEGRAM_ALLOWED_USERS`) can be silently dropped. Either add the
  Bale user id to `TELEGRAM_ALLOWED_USERS` as well, or override
  `_is_user_authorized_from_message` in the adapter.
- **A bot cannot message itself** — you cannot self-test the inbound loop via
  the API. Send a real message to your bot in the Bale app to confirm replies.
- **Bale is reachable directly from Iran** (no proxy needed). Only Telegram
  (`api.telegram.org`) is blocked without a proxy.
- **Imports** reference `plugins.platforms.telegram.adapter` and
  `gateway.platform_registry`, which resolve because the Hermes agent repo root
  is on `sys.path` when Hermes runs. This plugin is meant to be used alongside a
  Hermes checkout, not as a standalone pip package.

## Files

| File         | Purpose                                                |
|--------------|--------------------------------------------------------|
| `plugin.yaml`| Plugin manifest (name, env requirements, metadata)     |
| `__init__.py`| Exposes `register` (the plugin entry point)            |
| `adapter.py` | `BaleAdapter(TelegramAdapter)` — the actual adapter    |
