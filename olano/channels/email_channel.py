"""Email channel adapter — IMAP (inbound polling) + SMTP (outbound).

Config keys (from agent.json5 channels section):
  imap_host, imap_port (default 993), imap_user, imap_password
  smtp_host, smtp_port (default 587), smtp_user, smtp_password
  poll_interval_seconds (default 60)
  allowed_senders (list, optional — allowlist of email addresses)
  default_recipient (email address for outbound delivery)

Works with Gmail, Outlook, QQ Mail, AliMail, Fastmail, any IMAP/SMTP provider.
For Gmail: enable "App Passwords" or use OAuth via LIONCLAW_GOOGLE_TOKEN.
"""

from __future__ import annotations

import asyncio
import email as email_lib
import email.utils
import imaplib
import logging
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

from olano.channels.adapter import BaseChannelAdapter, NormalizedInbound

logger = logging.getLogger(__name__)


class EmailChannel(BaseChannelAdapter):
    """IMAP/SMTP email channel adapter."""

    channel_type = "email"
    capabilities = ["text", "attachments", "pulse_delivery"]

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__()
        self.config = config
        self._imap_host = config.get("imap_host", "")
        self._imap_port = int(config.get("imap_port", 993))
        self._imap_user = config.get("imap_user", "")
        self._imap_password = config.get("imap_password", "")
        self._smtp_host = config.get("smtp_host", self._imap_host)
        self._smtp_port = int(config.get("smtp_port", 587))
        self._smtp_user = config.get("smtp_user", self._imap_user)
        self._smtp_password = config.get("smtp_password", self._imap_password)
        self._poll_interval = int(config.get("poll_interval_seconds", 60))
        self._allowed_senders: list[str] = config.get("allowed_senders", [])
        self._default_recipient: str = config.get("default_recipient", self._imap_user)
        self._handler = None
        self._running = False
        self._seen_uids: set[str] = set()

    def set_handler(self, handler) -> None:
        self._handler = handler

    async def start(self) -> None:
        if not self._imap_host:
            logger.warning("EmailChannel: imap_host not configured — skipping.")
            return
        self._running = True
        asyncio.create_task(self._poll_loop(), name="email_poll")
        logger.info(
            "EmailChannel: started polling %s every %ds", self._imap_host, self._poll_interval
        )

    async def stop(self) -> None:
        self._running = False

    async def send_text(  # type: ignore[override]  # returns bool; base returns str
        self, text: str, target: str = "", **kwargs: Any
    ) -> bool:
        """Send an email via SMTP."""
        recipient = target or self._default_recipient
        if not recipient:
            logger.warning("EmailChannel.send_text: no recipient configured")
            return False
        try:
            await asyncio.get_event_loop().run_in_executor(
                None,
                self._smtp_send,
                recipient,
                text,
                kwargs.get("subject", "Message from Olano"),
            )
            return True
        except Exception as e:
            logger.error("EmailChannel: SMTP send failed: %s", e)
            return False

    def _smtp_send(self, to: str, body: str, subject: str) -> None:
        msg = MIMEMultipart()
        msg["From"] = self._smtp_user
        msg["To"] = to
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain", "utf-8"))

        context = ssl.create_default_context()
        if self._smtp_port == 465:
            with smtplib.SMTP_SSL(self._smtp_host, self._smtp_port, context=context) as server:
                server.login(self._smtp_user, self._smtp_password)
                server.sendmail(self._smtp_user, to, msg.as_string())
        else:
            with smtplib.SMTP(self._smtp_host, self._smtp_port) as server:
                server.ehlo()
                server.starttls(context=context)
                server.login(self._smtp_user, self._smtp_password)
                server.sendmail(self._smtp_user, to, msg.as_string())

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                messages = await asyncio.get_event_loop().run_in_executor(None, self._fetch_unread)
                for msg_data in messages:
                    await self._process_message(msg_data)
            except Exception as e:
                logger.debug("EmailChannel: poll error: %s", e)
            await asyncio.sleep(self._poll_interval)

    def _fetch_unread(self) -> list[dict]:
        """Fetch unread emails via IMAP. Returns list of message dicts."""
        messages = []
        try:
            mail = imaplib.IMAP4_SSL(self._imap_host, self._imap_port)
            mail.login(self._imap_user, self._imap_password)
            mail.select("INBOX")
            _, uids = mail.uid("search", None, "UNSEEN")  # type: ignore[arg-type]  # None=no charset
            uid_list = uids[0].split() if uids[0] else []
            for uid in uid_list[-10:]:  # Process at most 10 at a time
                uid_str = uid.decode()
                if uid_str in self._seen_uids:
                    continue
                _, data = mail.uid("fetch", uid, "(RFC822)")
                raw = data[0][1] if data and data[0] else None
                if raw:
                    msg = email_lib.message_from_bytes(raw)
                    sender = email_lib.utils.parseaddr(msg.get("From", ""))[1]
                    subject = msg.get("Subject", "")
                    body = self._extract_body(msg)
                    messages.append(
                        {
                            "uid": uid_str,
                            "from": sender,
                            "subject": subject,
                            "body": body,
                        }
                    )
                    self._seen_uids.add(uid_str)
            mail.logout()
        except Exception as e:
            logger.debug("EmailChannel: IMAP fetch error: %s", e)
        return messages

    def _extract_body(self, msg: email_lib.message.Message) -> str:
        """Extract plain text body from email message."""
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                ctype = part.get_content_type()
                if ctype == "text/plain":
                    payload = part.get_payload(decode=True)
                    if isinstance(payload, bytes):
                        body = payload.decode(
                            part.get_content_charset() or "utf-8", errors="replace"
                        )
                        break
        else:
            payload = msg.get_payload(decode=True)
            if isinstance(payload, bytes):
                body = payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
        return body.strip()

    async def _process_message(self, msg_data: dict) -> None:
        """Process an inbound email and route to handler."""
        sender = msg_data["from"]
        if self._allowed_senders and sender not in self._allowed_senders:
            logger.debug("EmailChannel: rejected sender %s (not in allowlist)", sender)
            return

        if not self._handler:
            return

        text = f"Subject: {msg_data['subject']}\n\n{msg_data['body']}"
        inbound = NormalizedInbound(
            text=text,
            sender_id=sender,
            thread_id=f"email:{sender}",
            channel="email",
            raw=msg_data,
        )
        try:
            response = await self._handler(inbound)
            if response:
                await self.send_text(
                    response,
                    target=sender,
                    subject=f"Re: {msg_data['subject']}",
                )
        except Exception as e:
            logger.error("EmailChannel: handler error for %s: %s", sender, e)
