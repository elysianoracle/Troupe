"""Telegram surface: text your agents like people, approve actions with one tap.

Long polling, so no public URL is needed. Only TROUPE_TELEGRAM_OWNER_ID can talk to the bot.
  /rooms            list rooms        /use <room name>  switch the active room
  /approvals        list pending      @name message     address one agent in a group room
Anything else goes to the active room, including /teach, /done, /stop, /pause, /resume.
"""
from __future__ import annotations

import asyncio
import json
import logging

import httpx

log = logging.getLogger("troupe.telegram")


class TelegramBot:
    def __init__(self, app):
        self.app = app
        self.base = f"https://api.telegram.org/bot{app.settings.telegram_token}"
        self.owner = str(app.settings.telegram_owner_id)
        self.http = httpx.AsyncClient(timeout=70)

    @property
    def room_id(self) -> str:
        rid = self.app.db.kv_get("telegram_room")
        if rid and self.app.db.one("SELECT id FROM rooms WHERE id = ?", (rid,)):
            return rid
        first = self.app.db.one("SELECT id FROM rooms ORDER BY created_at LIMIT 1")
        return first["id"] if first else ""

    async def api(self, method: str, **payload):
        try:
            r = await self.http.post(f"{self.base}/{method}", json=payload)
            return r.json()
        except (httpx.HTTPError, json.JSONDecodeError) as e:
            log.warning("telegram %s failed: %s", method, e)
            return {}

    async def say(self, text: str, **extra):
        if not self.owner:
            return
        for i in range(0, max(len(text), 1), 4000):
            await self.api("sendMessage", chat_id=self.owner, text=text[i:i + 4000], **extra)

    async def run(self):
        if not self.owner:
            log.error("TROUPE_TELEGRAM_OWNER_ID is not set; the Telegram bot will ignore everyone")
        await asyncio.gather(self.poll(), self.forward_events())

    async def poll(self):
        offset = 0
        while True:
            data = await self.api("getUpdates", offset=offset, timeout=50,
                                  allowed_updates=["message", "callback_query"])
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                try:
                    await self.handle(upd)
                except Exception:
                    log.exception("telegram update failed")
            if not data:
                await asyncio.sleep(5)

    async def handle(self, upd: dict):
        if "callback_query" in upd:
            cq = upd["callback_query"]
            if str(cq["from"]["id"]) != self.owner:
                return
            _, aid, verdict = cq["data"].split(":")
            a = self.app.decide_approval(aid, verdict == "y")
            await self.api("answerCallbackQuery", callback_query_id=cq["id"], text=a["status"])
            await self.api("editMessageReplyMarkup", chat_id=self.owner, message_id=cq["message"]["message_id"],
                           reply_markup={"inline_keyboard": []})
            return
        msg = upd.get("message") or {}
        if str(msg.get("from", {}).get("id")) != self.owner or "text" not in msg:
            return
        text = msg["text"].strip()
        db = self.app.db
        if text == "/start":
            return await self.say("Troupe is connected. /rooms to pick a room, then just talk.")
        if text == "/rooms":
            rooms = db.all("SELECT id, name, kind FROM rooms ORDER BY created_at")
            cur = self.room_id
            return await self.say("\n".join(f"{'▶' if r['id'] == cur else ' '} {r['name']} ({r['kind']})"
                                            for r in rooms) or "no rooms yet")
        if text.startswith("/use "):
            name = text[5:].strip().lower()
            r = db.one("SELECT id, name FROM rooms WHERE lower(name) = ?", (name,))
            if not r:
                return await self.say("no room with that name")
            db.kv_set("telegram_room", r["id"])
            return await self.say(f"Now talking in {r['name']}")
        if text == "/approvals":
            rows = db.all("SELECT * FROM approvals WHERE status = 'pending'")
            if not rows:
                return await self.say("nothing pending")
            for r in rows:
                await self.send_approval({"id": r["id"], "tool": r["tool"], "args": json.loads(r["args"]),
                                          "reason": r["reason"], "agent_name": ""})
            return
        if not self.room_id:
            return await self.say("Create an agent in the web UI first.")
        await self.app.orch.user_message(self.room_id, text)

    async def send_approval(self, d: dict):
        args = json.dumps(d["args"], indent=1)[:1500]
        await self.say(f"🔐 {d.get('agent_name', '')} wants to run {d['tool']}\nWhy asking: {d['reason']}\n\n{args}",
                       reply_markup={"inline_keyboard": [[{"text": "✅ Approve", "callback_data": f"ap:{d['id']}:y"},
                                                          {"text": "❌ Deny", "callback_data": f"ap:{d['id']}:n"}]]})

    async def forward_events(self):
        q = self.app.events.subscribe()
        while True:
            ev = await q.get()
            try:
                d = ev["data"]
                if ev["type"] == "approval":
                    await self.send_approval(d)
                elif ev["type"] == "message" and d["sender_type"] != "user" and d["room_id"] == self.room_id:
                    who = d.get("agent_name") or "system"
                    await self.say(f"{who}: {d['content']}")
                elif ev["type"] == "message" and d["sender_type"] == "agent" and d["meta"].get("kind") == "question":
                    room = self.app.db.one("SELECT name FROM rooms WHERE id = ?", (d["room_id"],))
                    await self.say(f"❓ {d.get('agent_name')} in {room['name'] if room else '?'}: {d['content']}\n"
                                   f"(/use {room['name'] if room else ''} to answer)")
            except Exception:
                log.exception("telegram forward failed")
