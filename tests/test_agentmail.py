"""AgentMail integration against an in-memory fake of the documented REST API."""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from troupe import triggers
from troupe.agentmail import AgentMail
from troupe.app import App
from troupe.config import load_settings
from troupe.llm import MockProvider


class FakeAgentMail:
    """Implements the endpoints Troupe uses: inboxes create, messages list/get/send/reply/update."""

    def __init__(self, taken: set[str] | None = None):
        self.inboxes: dict[str, dict] = {}
        self.messages: dict[str, list[dict]] = {}
        self.taken = taken or set()
        self.calls: list[tuple[str, str]] = []
        self.clock = datetime(2026, 10, 16, tzinfo=timezone.utc)
        self.n = 0

    def ts(self) -> str:
        self.clock += timedelta(seconds=1)
        return self.clock.isoformat().replace("+00:00", "Z")

    def deliver(self, to_email: str, frm: str, subject: str, text: str, in_reply_to: str | None = None) -> str:
        self.n += 1
        mid = f"<msg{self.n}@agentmail.to>"
        box = next(b for b in self.inboxes.values() if b["email"] == to_email)
        self.messages[box["inbox_id"]].append({
            "inbox_id": box["inbox_id"], "thread_id": f"thr{self.n}", "message_id": mid, "labels": ["received", "unread"],
            "timestamp": self.ts(), "from": frm, "to": [to_email], "subject": subject, "text": text,
            "extracted_text": text, "preview": text[:50], "in_reply_to": in_reply_to})
        return mid

    def handler(self, req: httpx.Request) -> httpx.Response:
        assert req.headers["authorization"] == "Bearer am_test_key"
        path, method = req.url.path, req.method
        self.calls.append((method, path))
        body = json.loads(req.content) if req.content else {}
        if method == "POST" and path == "/v0/inboxes":
            if re.search(r"[()]", body.get("display_name", "")):  # the real API rejects these
                return httpx.Response(400, json={"name": "ValidationError", "code": "validation_error",
                                                 "message": "Request validation failed", "errors": [
                                                     {"path": ["display_name"], "message": "invalid character(s): ( )"}]})
            email = f"{body['username']}@agentmail.to"
            if email in self.taken or any(b["email"] == email for b in self.inboxes.values()):
                return httpx.Response(409, json={"name": "AlreadyExistsError", "message": "inbox exists"})
            box = {"inbox_id": email, "email": email, "pod_id": "pod1", "created_at": self.ts(),
                   "updated_at": self.ts(), "display_name": body.get("display_name")}
            self.inboxes[email] = box
            self.messages[email] = []
            return httpx.Response(200, json=box)
        m = re.fullmatch(r"/v0/inboxes/([^/]+)/messages", path)
        if m and method == "GET":
            after = req.url.params.get("after", "")
            items = [dict(x) for x in self.messages[m[1]] if x["timestamp"] >= after]
            for x in items:
                x.pop("text"); x.pop("extracted_text")
            return httpx.Response(200, json={"count": len(items), "messages": items})
        m = re.fullmatch(r"/v0/inboxes/([^/]+)/messages/send", path)
        if m:
            box = self.inboxes[m[1]]
            self.n += 1
            mid = f"<sent{self.n}@agentmail.to>"
            self.messages[m[1]].append({"inbox_id": m[1], "thread_id": "t", "message_id": mid, "labels": ["sent"],
                                        "timestamp": self.ts(), "from": f"{box['display_name']} <{box['email']}>",
                                        "to": body["to"], "subject": body["subject"], "text": body["text"]})
            for addr in body["to"]:  # deliver internally between fake inboxes
                if any(b["email"] == addr for b in self.inboxes.values()):
                    self.deliver(addr, box["email"], body["subject"], body["text"])
            return httpx.Response(200, json={"message_id": mid, "thread_id": "t"})
        m = re.fullmatch(r"/v0/inboxes/([^/]+)/messages/([^/]+)/reply", path)
        if m:
            self.replies = getattr(self, "replies", []) + [{"inbox": m[1], "message_id": m[2], **body}]
            return httpx.Response(200, json={"message_id": "<reply@agentmail.to>", "thread_id": "t"})
        m = re.fullmatch(r"/v0/inboxes/([^/]+)/messages/([^/]+)", path)
        if m and method == "GET":
            msg = next(x for x in self.messages[m[1]] if x["message_id"] == m[2])
            return httpx.Response(200, json=msg)
        if m and method == "PATCH":
            msg = next(x for x in self.messages[m[1]] if x["message_id"] == m[2])
            msg["labels"] = [l for l in msg["labels"] if l not in body.get("remove_labels", [])] + body.get("add_labels", [])
            return httpx.Response(200, json={"message_id": m[2], "labels": msg["labels"]})
        return httpx.Response(404, json={"name": "NotFoundError", "message": path})


def text_of(msg) -> str:
    c = msg["content"]
    return c if isinstance(c, str) else "\n".join(b.get("text", "") for b in c if b.get("type") == "text")


@pytest.fixture
async def env(tmp_path, monkeypatch):
    monkeypatch.setenv("TROUPE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TROUPE_SANDBOX", "local")
    monkeypatch.setenv("TROUPE_LLM_PROVIDER", "mock")
    monkeypatch.setenv("TROUPE_ENV_FILE", str(tmp_path / "none.env"))
    monkeypatch.setenv("TROUPE_AGENTMAIL_PREFIX", "tst")
    app = App(load_settings(), llm=MockProvider())
    fake = FakeAgentMail(taken={"tst-scout@agentmail.to"})
    app.email.agentmail = AgentMail(app.settings, app.db, app.vault, transport=httpx.MockTransport(fake.handler))
    disp = asyncio.create_task(app.orch.dispatcher())
    yield app, fake
    disp.cancel()


async def settle(app):
    for _ in range(200):
        await asyncio.sleep(0.05)
        if not app.db.one("SELECT COUNT(*) n FROM tasks WHERE status IN ('queued','running')")["n"] and not app.orch.running:
            return


async def test_disabled_until_key_is_in_vault(env):
    app, fake = env
    assert app.email.provider == ""
    assert "send_email" not in {t.name for t in app.tools_for(app.create_agent("Scout"))}
    app.vault.set("agentmail_api_key", "am_test_key")
    assert app.email.provider == "agentmail"
    assert "send_email" in {t.name for t in app.tools_for(app.db.one("SELECT * FROM agents"))}


async def test_inboxes_created_with_conflict_fallback(env):
    app, fake = env
    app.vault.set("agentmail_api_key", "am_test_key")
    chief = app.create_agent("Chief")
    scout = app.create_agent("Scout")
    await app.email.agentmail.ensure_all()
    assert app.email.address_for(chief) == "tst-chief@agentmail.to"
    # tst-scout was taken, so a suffix is added
    assert app.email.address_for(scout).startswith("tst-scout-") and app.email.address_for(scout).endswith("@agentmail.to")
    await app.email.agentmail.ensure_all()  # idempotent: no new inboxes
    assert len(fake.inboxes) == 2
    assert "Chief AI Assistant" == fake.inboxes["tst-chief@agentmail.to"]["display_name"]


async def test_inbound_mail_becomes_one_tainted_task_and_reply_threads(env):
    app, fake = env
    app.vault.set("agentmail_api_key", "am_test_key")
    seen_prompts = []

    def handler(system, messages, tools):
        last = text_of(messages[-1])
        seen_prompts.append(last)
        if "untrusted_email" in last:
            mid = re.search(r"Message-ID: (\S+)", last).group(1).strip("<>")  # models drop the brackets
            return {"content": [{"type": "tool_use", "id": "r1", "name": "send_email", "input": {
                "to": "alice@example.com", "subject": "Re: Lunch?", "body": "Tuesday works.", "in_reply_to": mid}}],
                "stop_reason": "tool_use"}
        return {"content": [{"type": "text", "text": "Replied to Alice."}], "stop_reason": "end_turn"}

    app.llm.handler = handler
    agent = app.create_agent("Courier", contacts=["alice@example.com"])
    await app.email.agentmail.ensure_all()
    addr = app.email.address_for(agent)
    mid = fake.deliver(addr, "Alice <alice@example.com>", "Lunch?", "Are you free Tuesday?")

    await triggers.agentmail_poll(app)
    await triggers.agentmail_poll(app)  # second poll must not duplicate
    tasks = app.db.all("SELECT * FROM tasks")
    assert len(tasks) == 1 and tasks[0]["source"] == "email" and tasks[0]["tainted"] == 1
    assert "Are you free Tuesday?" in tasks[0]["input"]
    msg = fake.messages[addr][0]
    assert "read" in msg["labels"] and "unread" not in msg["labels"]

    await settle(app)
    # Tainted task: even a known contact needs approval before sending.
    appr = app.db.one("SELECT * FROM approvals")
    assert appr and "untrusted" in appr["reason"]
    app.decide_approval(appr["id"], True)
    await settle(app)
    assert fake.replies[0]["message_id"] == mid and "Tuesday works." in fake.replies[0]["text"]
    assert "AI assistant" in fake.replies[0]["text"]  # disclosure footer


async def test_teammates_email_each_other_without_approval(env):
    app, fake = env
    app.vault.set("agentmail_api_key", "am_test_key")
    courier = app.create_agent("Courier")
    chief = app.create_agent("Chief")
    await app.email.agentmail.ensure_all()
    chief_addr = app.email.address_for(chief)

    def handler(system, messages, tools):
        last = text_of(messages[-1])
        if system.startswith("You are Courier") and "Owner says" in last:
            return {"content": [{"type": "tool_use", "id": "s1", "name": "send_email", "input": {
                "to": chief_addr, "subject": "Status", "body": "Inbox is clear."}}], "stop_reason": "tool_use"}
        return {"content": [{"type": "text", "text": "Done."}], "stop_reason": "end_turn"}

    app.llm.handler = handler
    await app.orch.user_message(app.dm_room(courier["id"])["id"], "email Chief a status update")
    await settle(app)
    assert app.db.one("SELECT COUNT(*) n FROM approvals")["n"] == 0
    assert any(p.endswith("/messages/send") for _, p in fake.calls)
    # Chief receives it on its next poll; Courier's own sent copy is not turned into a task.
    await triggers.agentmail_poll(app)
    email_tasks = app.db.all("SELECT agent_id FROM tasks WHERE source = 'email'")
    assert [t["agent_id"] for t in email_tasks] == [chief["id"]]
