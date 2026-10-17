"""AgentMail: a real, dedicated email inbox for every agent (https://agentmail.to).

Each agent gets its own address (e.g. troupe-ab12-scout@agentmail.to, or on your own verified
domain). Mail sent to it becomes an untrusted task for that agent; agents send and reply from it.
The API key is read from the vault entry "agentmail_api_key" (Vault tab in the UI), falling back
to the AGENTMAIL_API_KEY environment variable, so the model never sees it.

Inbound mail is fetched by polling, so the server needs no public URL or webhook.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from typing import Any

import httpx
from urllib.parse import quote

log = logging.getLogger("troupe.agentmail")
FOOTER = ("\n\n--\nSent by {name}, an AI assistant acting on behalf of its owner. "
          "Reply to this address to reach them.")


class AgentMailError(RuntimeError):
    pass


class AgentMail:
    def __init__(self, settings, db, vault, transport: httpx.AsyncBaseTransport | None = None):
        self.s = settings
        self.db = db
        self.vault = vault
        self.base = settings.agentmail_base_url.rstrip("/")
        self.http = httpx.AsyncClient(timeout=30, transport=transport)

    # --- config -------------------------------------------------------------------------------
    def api_key(self) -> str:
        return (self.vault.get("agentmail_api_key") or self.s.agentmail_api_key or "").strip()

    @property
    def enabled(self) -> bool:
        return bool(self.api_key())

    def prefix(self) -> str:
        if self.s.agentmail_prefix:
            return self.s.agentmail_prefix
        # Stable per install, so addresses don't collide with other people's on agentmail.to.
        return "troupe-" + hashlib.sha256(self.s.admin_token.encode()).hexdigest()[:4]

    async def _req(self, method: str, path: str, **kw) -> Any:
        key = self.api_key()
        if not key:
            raise AgentMailError("AgentMail is not configured (add 'agentmail_api_key' in the Vault)")
        r = await self.http.request(method, f"{self.base}{path}", headers={"Authorization": f"Bearer {key}"}, **kw)
        if r.status_code >= 400:
            try:
                err = r.json()
                msg = f"{r.status_code} {err.get('name', '')}: {err.get('message', r.text)}"
                if err.get("code"):
                    msg += f" [{err['code']}]"
                for fe in err.get("errors") or []:
                    msg += f" | {'.'.join(map(str, fe.get('path', [])))}: {fe.get('message', '')}"
                if err.get("fix"):
                    msg += f" - {err['fix'][:300]}"
            except ValueError:
                msg = f"HTTP {r.status_code}: {r.text[:300]}"
            raise AgentMailError(msg)
        return r.json() if r.content else {}

    # --- inboxes ----------------------------------------------------------------------------------
    def inbox(self, agent_id: str) -> dict | None:
        raw = self.db.kv_get(f"agentmail:{agent_id}")
        return json.loads(raw) if raw else None

    def address_for(self, agent: dict) -> str:
        box = self.inbox(agent["id"])
        return box["email"] if box else ""

    async def ensure_inbox(self, agent: dict) -> dict:
        box = self.inbox(agent["id"])
        if box:
            return box
        username = f"{self.prefix()}-{re.sub(r'[^a-z0-9-]', '-', agent['name'].lower())}"
        body: dict[str, Any] = {"username": username, "display_name": f"{agent['name']} AI Assistant",  # AgentMail rejects ( ) here
                                "client_id": f"troupe-{agent['id']}"}
        if self.s.agentmail_domain:
            body["domain"] = self.s.agentmail_domain
        try:
            data = await self._req("POST", "/inboxes", json=body)
        except AgentMailError as e:
            if not str(e).startswith(("409", "400")):
                raise
            body["username"] = f"{username}-{agent['id'][-4:]}"  # address taken: add a suffix
            data = await self._req("POST", "/inboxes", json=body)
        box = {"inbox_id": data["inbox_id"], "email": data["email"], "cursor": data.get("created_at", "")}
        self.db.kv_set(f"agentmail:{agent['id']}", json.dumps(box))
        self.db.audit("system", "agentmail_inbox", {"email": box["email"]}, agent_id=agent["id"])
        log.info("AgentMail inbox for %s: %s", agent["name"], box["email"])
        return box

    async def ensure_all(self) -> list[dict]:
        """Create missing inboxes; returns the agents that have one. After a failure (e.g. the plan's
        inbox limit) an agent is retried hourly, not on every poll."""
        ready = []
        for agent in self.db.all("SELECT * FROM agents"):
            if self.inbox(agent["id"]):
                ready.append(agent)
                continue
            failed_at = float(self.db.kv_get(f"agentmail_fail:{agent['id']}", "0") or 0)
            if time.time() - failed_at < 3600:
                continue
            try:
                await self.ensure_inbox(agent)
                ready.append(agent)
            except Exception as e:
                self.db.kv_set(f"agentmail_fail:{agent['id']}", str(time.time()))
                log.warning("could not create AgentMail inbox for %s (retrying in 1h): %s", agent["name"], e)
        return ready

    def team_addresses(self) -> set[str]:
        out = set()
        for row in self.db.all("SELECT value FROM kv WHERE key LIKE 'agentmail:%'"):
            try:
                out.add(json.loads(row["value"])["email"].lower())
            except (ValueError, KeyError):
                pass
        return out

    # --- sending -------------------------------------------------------------------------------------
    async def send(self, agent: dict, to: str, subject: str, body: str, in_reply_to: str | None = None) -> str:
        box = await self.ensure_inbox(agent)
        text = body + FOOTER.format(name=agent["name"])
        recipients = [x.strip() for x in re.split(r"[,;]", to) if x.strip()]
        inbox = quote(box["inbox_id"], safe="")
        if in_reply_to:
            # The message id from the inbound email: reply in the same thread. Models often drop the
            # angle brackets of "<id@host>", so restore them.
            mid = in_reply_to.strip()
            if not mid.startswith("<"):
                mid = f"<{mid.strip('<>')}>"
            try:
                data = await self._req("POST", f"/inboxes/{inbox}/messages/{quote(mid, safe='')}/reply",
                                       json={"text": text, "to": recipients})
                return data.get("message_id", "")
            except AgentMailError as e:
                if not str(e).startswith("404"):
                    raise
                log.info("reply target %s not found; sending as a new message", mid)
                subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
        data = await self._req("POST", f"/inboxes/{inbox}/messages/send",
                               json={"to": recipients, "subject": subject, "text": text,
                                     "headers": {"Auto-Submitted": "auto-generated"}})
        return data.get("message_id", "")

    # --- receiving -----------------------------------------------------------------------------------
    async def fetch_new(self, agent: dict) -> list[dict]:
        """New inbound messages since the last poll, oldest first. Marks them read."""
        box = await self.ensure_inbox(agent)
        params: dict[str, Any] = {"ascending": "true", "limit": 25}
        if box.get("cursor"):
            params["after"] = box["cursor"]
        data = await self._req("GET", f"/inboxes/{quote(box['inbox_id'], safe='')}/messages", params=params)
        seen = set(box.get("seen", []))
        out = []
        for item in data.get("messages", []):
            mid = item["message_id"]
            box["cursor"] = max(box.get("cursor", ""), item.get("timestamp", ""))
            if mid in seen:
                continue
            seen.add(mid)
            if box["email"].lower() in (item.get("from") or "").lower():
                continue  # our own sent mail
            full = await self._req("GET", f"/inboxes/{quote(box['inbox_id'], safe='')}/messages/{quote(mid, safe='')}")
            body = full.get("extracted_text") or full.get("text") or full.get("preview") or ""
            out.append({"message_id": mid, "thread_id": full.get("thread_id", ""), "from": full.get("from", ""),
                        "to": full.get("to", []), "subject": full.get("subject", ""),
                        "date": full.get("timestamp", ""), "body": body[:20_000],
                        "attachments": [a.get("filename") or a.get("attachment_id") for a in full.get("attachments") or []]})
            try:
                await self._req("PATCH", f"/inboxes/{quote(box['inbox_id'], safe='')}/messages/{quote(mid, safe='')}",
                                json={"add_labels": ["read"], "remove_labels": ["unread"]})
            except AgentMailError:
                pass
        box["seen"] = list(seen)[-200:]
        self.db.kv_set(f"agentmail:{agent['id']}", json.dumps(box))
        return out
