"""Agent identities: email (AgentMail inboxes, or any IMAP/SMTP mailbox) and optional SMS (Twilio).

With an AgentMail API key, every agent gets its own real inbox (see agentmail.py). Otherwise:

Email uses plus-addressing so one mailbox serves the whole team: if TROUPE_EMAIL_ADDRESS is
me@example.com, the agent "research" sends and receives as me+research@example.com. Forward or CC
mail to that address and it becomes a task for that agent. Works with Fastmail, Migadu, Purelymail,
a self-hosted Postfix/Dovecot, or Gmail with an app password.
"""
from __future__ import annotations

import asyncio
import base64
import email
import email.utils
import hashlib
import hmac
import imaplib
import logging
import re
import smtplib
from email.message import EmailMessage
from email.policy import default as default_policy

import httpx

log = logging.getLogger("troupe.comms")
FOOTER = ("\n\n--\nSent by {name}, an AI assistant acting on behalf of its owner. "
          "Reply to this address to reach them.")


def _html_to_text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style).*?</\1>", "", html)
    html = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>", "\n", html)
    text = re.sub(r"<[^>]+>", "", html)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


class Email:
    def __init__(self, settings):
        self.s = settings

    @property
    def enabled(self) -> bool:
        return bool(self.s.email_address and self.s.smtp_host)

    def address_for(self, agent: dict) -> str:
        if not self.s.email_address:
            return ""
        local, _, domain = self.s.email_address.partition("@")
        return f"{local}+{agent['name'].lower()}@{domain}"

    async def send(self, agent: dict, to: str, subject: str, body: str, in_reply_to: str | None = None) -> str:
        if not self.enabled:
            raise RuntimeError("email is not configured (TROUPE_EMAIL_ADDRESS / TROUPE_SMTP_HOST)")
        msg = EmailMessage()
        msg["From"] = email.utils.formataddr((f"{agent['name']} (AI assistant)", self.address_for(agent)))
        msg["To"] = to
        msg["Subject"] = subject
        msg["Message-ID"] = email.utils.make_msgid(domain=self.s.email_address.partition("@")[2] or None)
        msg["Auto-Submitted"] = "auto-generated"
        msg["X-Troupe-Agent"] = agent["name"]
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = in_reply_to
        msg.set_content(body + FOOTER.format(name=agent["name"]))

        def _send():
            with smtplib.SMTP(self.s.smtp_host, self.s.smtp_port, timeout=30) as smtp:
                smtp.starttls()
                smtp.login(self.s.smtp_user or self.s.email_address, self.s.smtp_password)
                smtp.send_message(msg)

        await asyncio.to_thread(_send)
        return msg["Message-ID"]

    def _fetch_unseen(self) -> list[dict]:
        out = []
        box = imaplib.IMAP4_SSL(self.s.imap_host)
        try:
            box.login(self.s.imap_user or self.s.email_address, self.s.imap_password)
            box.select("INBOX")
            _, data = box.search(None, "UNSEEN")
            for num in (data[0].split() if data and data[0] else [])[:25]:
                _, parts = box.fetch(num, "(RFC822)")
                raw = parts[0][1]
                m = email.message_from_bytes(raw, policy=default_policy)
                rcpts = " ".join(str(m.get(h, "")) for h in ("To", "Cc", "Delivered-To", "X-Original-To"))
                local = self.s.email_address.partition("@")[0]
                tags = re.findall(rf"{re.escape(local)}\+([\w\-]+)@", rcpts, re.I)
                body_part = m.get_body(preferencelist=("plain", "html"))
                body = body_part.get_content() if body_part else ""
                if body_part is not None and body_part.get_content_type() == "text/html":
                    body = _html_to_text(body)
                attachments = [p.get_filename() for p in m.iter_attachments() if p.get_filename()]
                out.append({"tag": tags[0].lower() if tags else "", "from": str(m.get("From", "")),
                            "subject": str(m.get("Subject", "")), "message_id": str(m.get("Message-ID", "")),
                            "date": str(m.get("Date", "")), "body": body[:20_000], "attachments": attachments})
                box.store(num, "+FLAGS", "\\Seen")
        finally:
            try:
                box.logout()
            except Exception:
                pass
        return out

    async def fetch_unseen(self) -> list[dict]:
        if not (self.s.imap_host and self.s.email_address):
            return []
        return await asyncio.to_thread(self._fetch_unseen)


class SMS:
    """Twilio SMS. US numbers need A2P 10DLC registration before texting works; get consent first."""

    def __init__(self, settings):
        self.s = settings

    @property
    def enabled(self) -> bool:
        return bool(self.s.twilio_sid and self.s.twilio_token and self.s.twilio_from)

    async def send(self, to: str, body: str) -> str:
        if not self.enabled:
            raise RuntimeError("SMS is not configured (TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_FROM_NUMBER)")
        url = f"https://api.twilio.com/2010-04-01/Accounts/{self.s.twilio_sid}/Messages.json"
        async with httpx.AsyncClient(timeout=30) as http:
            r = await http.post(url, data={"To": to, "From": self.s.twilio_from, "Body": body[:1500]},
                                auth=(self.s.twilio_sid, self.s.twilio_token))
            r.raise_for_status()
            return r.json().get("sid", "")

    def valid_signature(self, url: str, params: dict, signature: str) -> bool:
        payload = url + "".join(k + params[k] for k in sorted(params))
        digest = hmac.new(self.s.twilio_token.encode(), payload.encode(), hashlib.sha1).digest()
        return hmac.compare_digest(base64.b64encode(digest).decode(), signature or "")


class Mailer:
    """Picks the email backend: AgentMail when its API key is set (checked live, so adding the key
    in the Vault takes effect without a restart), else IMAP/SMTP plus-addressing."""

    def __init__(self, smtp: Email, agentmail):
        self.smtp = smtp
        self.agentmail = agentmail

    @property
    def provider(self) -> str:
        if self.agentmail.enabled:
            return "agentmail"
        return "smtp" if self.smtp.enabled else ""

    @property
    def enabled(self) -> bool:
        return bool(self.provider)

    def address_for(self, agent: dict) -> str:
        if self.agentmail.enabled:
            return self.agentmail.address_for(agent)
        return self.smtp.address_for(agent)

    async def send(self, agent: dict, to: str, subject: str, body: str, in_reply_to: str | None = None) -> str:
        if self.agentmail.enabled:
            return await self.agentmail.send(agent, to, subject, body, in_reply_to)
        return await self.smtp.send(agent, to, subject, body, in_reply_to)
