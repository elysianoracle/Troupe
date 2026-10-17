"""Ways agents start work unprompted: cron schedules and the email inbox.
(Webhooks and inbound SMS arrive through HTTP routes in surfaces/web.py.)"""
from __future__ import annotations

import asyncio
import logging

from croniter import croniter

from .db import now

log = logging.getLogger("troupe.triggers")


async def scheduler(app) -> None:
    db = app.db
    while True:
        await asyncio.sleep(20)
        try:
            if app.guard.paused():
                continue
            for s in db.all("SELECT * FROM schedules WHERE enabled = 1 AND next_run <= ?", (now(),)):
                nxt = croniter(s["cron"], now()).get_next(float)
                db.update("schedules", "id", s["id"], {"next_run": nxt, "last_run": now()})
                agent = db.one("SELECT * FROM agents WHERE id = ?", (s["agent_id"],))
                if not agent or agent["status"] != "active":
                    continue
                db.audit("trigger", "schedule", {"schedule": s["id"]}, agent_id=agent["id"])
                app.orch.create_task(agent["id"], s["room_id"], "schedule", s["prompt"])
        except Exception:
            log.exception("scheduler error")


async def email_poller(app) -> None:
    db = app.db
    first = True
    while True:
        await asyncio.sleep(5 if first else app.settings.email_poll_seconds)
        first = False
        try:
            if app.email.agentmail.enabled:
                await agentmail_poll(app)
                continue
            if not app.settings.imap_host:
                continue
            for mail in await app.email.smtp.fetch_unseen():
                agents = db.all("SELECT * FROM agents ORDER BY created_at")
                agent = next((a for a in agents if a["name"].lower() == mail["tag"]), None) \
                    or (agents[0] if agents else None)
                if not agent:
                    continue
                room = app.dm_room(agent["id"])
                body = (f"From: {mail['from']}\nSubject: {mail['subject']}\nDate: {mail['date']}\n"
                        f"Message-ID: {mail['message_id']}\n"
                        + (f"Attachments: {', '.join(mail['attachments'])}\n" if mail["attachments"] else "")
                        + f"\n{mail['body']}")
                db.audit("trigger", "email", {"from": mail["from"], "subject": mail["subject"]}, agent_id=agent["id"])
                app.orch.post(room["id"], "system", "", f"📧 New email for {agent['name']} from {mail['from']}: "
                                                        f"{mail['subject']}")
                app.orch.create_task(agent["id"], room["id"], "email", body, tainted=True,
                                     state={"in_reply_to": mail["message_id"]})
        except Exception:
            log.exception("email poll error")


async def agentmail_poll(app) -> None:
    db, am = app.db, app.email.agentmail
    for agent in await am.ensure_all():
        if agent["status"] != "active":
            continue
        try:
            mails = await am.fetch_new(agent)
        except Exception as e:
            log.warning("AgentMail poll for %s failed: %s", agent["name"], e)
            continue
        for mail in mails:
            room = app.dm_room(agent["id"])
            body = (f"From: {mail['from']}\nTo: {', '.join(mail['to'])}\nSubject: {mail['subject']}\n"
                    f"Date: {mail['date']}\nMessage-ID: {mail['message_id']}  (pass as in_reply_to to reply in-thread)\n"
                    + (f"Attachments: {', '.join(mail['attachments'])}\n" if mail["attachments"] else "")
                    + f"\n{mail['body']}")
            db.audit("trigger", "email", {"from": mail["from"], "subject": mail["subject"], "via": "agentmail"},
                     agent_id=agent["id"])
            app.orch.post(room["id"], "system", "", f"\U0001F4E7 New email for {agent['name']} from {mail['from']}: "
                                                    f"{mail['subject']}")
            app.orch.create_task(agent["id"], room["id"], "email", body, tainted=True,
                                 state={"in_reply_to": mail["message_id"]})
