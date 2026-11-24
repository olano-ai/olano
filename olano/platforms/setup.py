"""Interactive per-adapter channel setup (prompts + verification)."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class ChannelSetupAdapter(Protocol):
    def setup_interactive(self, home: Path) -> dict:
        """Prompt for credentials, verify connection, return config dict."""
        ...


def setup_telegram(home: Path) -> dict:
    """Interactive setup for Telegram bot channel."""
    import click

    token = click.prompt("Telegram bot token (from @BotFather)")
    # Verify the token via getMe
    _verify_telegram(token)
    # platform:telegram is the maintained adapter; the bare "telegram" type
    # boots the deprecated duplicate (see channels/telegram_channel.py).
    return {"type": "platform:telegram", "token": token}


def setup_slack(home: Path) -> dict:
    import click

    token = click.prompt("Slack bot token (starts with xoxb-)")
    _verify_slack(token)
    # Same reasoning as setup_telegram: the bare "slack" type boots the
    # deprecated duplicate. Both adapters read the bot token off config.token
    # and take the app token from the vault/env, so the type is the only change.
    return {"type": "platform:slack", "token": token}


def setup_discord(home: Path) -> dict:
    import click

    token = click.prompt("Discord bot token")
    _verify_discord(token)
    return {"type": "platform:discord", "token": token}


def setup_email(home: Path) -> dict:
    import click

    smtp_host = click.prompt("SMTP host")
    smtp_port = click.prompt("SMTP port", default=587)
    username = click.prompt("SMTP username")
    password = click.prompt("SMTP password", hide_input=True)
    _verify_email(smtp_host, int(smtp_port), username, password)
    # NOT switched to platform:email with the others: that adapter takes its
    # hosts and credentials from EMAIL_* env vars, so the values prompted for
    # here would never reach it. The classic type is deprecated but is the one
    # that reads a config dict. Fixing this means teaching platform/email.py to
    # read its config entry (and dropping the username/password keys below,
    # which channels/email_channel.py already ignores — it wants imap_user /
    # imap_password), which is more than a type swap.
    return {
        "type": "email",
        "smtp_host": smtp_host,
        "smtp_port": int(smtp_port),
        "username": username,
        "password": password,
    }


def get_setup_fn(channel_type: str):
    _MAP = {
        "telegram": setup_telegram,
        "slack": setup_slack,
        "discord": setup_discord,
        "email": setup_email,
    }
    fn = _MAP.get(channel_type)
    if fn is None:
        raise ValueError(f"Unknown channel type: {channel_type!r}. Valid: {list(_MAP)}")
    return fn


# ── Verification helpers (raise on failure) ───────────────────────────────────


def _verify_telegram(token: str) -> None:
    import urllib.error
    import urllib.request

    url = f"https://api.telegram.org/bot{token}/getMe"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            import json

            data = json.load(resp)
            if not data.get("ok"):
                raise ValueError("Telegram API returned ok=false")
    except urllib.error.URLError as e:
        raise ValueError(f"Could not reach Telegram API: {e}") from e


def _verify_slack(token: str) -> None:
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        "https://slack.com/api/auth.test",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            import json

            data = json.load(resp)
            if not data.get("ok"):
                raise ValueError(f"Slack auth.test failed: {data.get('error')}")
    except urllib.error.URLError as e:
        raise ValueError(f"Could not reach Slack API: {e}") from e


def _verify_discord(token: str) -> None:
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        "https://discord.com/api/v10/users/@me",
        headers={"Authorization": f"Bot {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status not in (200, 201):
                raise ValueError(f"Discord API returned {resp.status}")
    except urllib.error.URLError as e:
        raise ValueError(f"Could not reach Discord API: {e}") from e


def _verify_email(host: str, port: int, username: str, password: str) -> None:
    import smtplib

    try:
        with smtplib.SMTP(host, port, timeout=5) as smtp:
            smtp.starttls()
            smtp.login(username, password)
    except Exception as e:
        raise ValueError(f"Email verification failed: {e}") from e
